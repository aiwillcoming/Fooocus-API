import copy
import random
import time
import numpy as np
import torch
from typing import List
from fooocusapi.parameters import inpaint_model_version, GenerationFinishReason, ImageGenerationParams, ImageGenerationResult
from fooocusapi.task_queue import TaskQueue, TaskType

save_log = True
task_queue = TaskQueue()


@torch.no_grad()
@torch.inference_mode()
def process_generate(params: ImageGenerationParams) -> List[ImageGenerationResult]:
    import modules.default_pipeline as pipeline
    import modules.patch as patch
    import modules.flags as flags
    import modules.core as core
    import modules.inpaint_worker as inpaint_worker
    import modules.path as path
    import modules.virtual_memory as virtual_memory
    import comfy.model_management as model_management
    from modules.util import join_prompts, remove_empty_str, image_is_generated_in_current_ui, resize_image, HWC3
    from modules.private_logger import log
    from modules.upscaler import perform_upscale
    from modules.expansion import safe_str
    from modules.sdxl_styles import apply_style, fooocus_expansion, aspect_ratios

    outputs = []

    def progressbar(number, text):
        print(f'[Fooocus] {text}')
        outputs.append(['preview', (number, text, None)])

    def make_results_from_outputs():
        results: List[ImageGenerationResult] = []
        for item in outputs:
            if item[0] == 'results':
                for im in item[1]:
                    if isinstance(im, np.ndarray):
                        results.append(ImageGenerationResult(im=im, seed=item[2], finish_reason=GenerationFinishReason.success))
        return results

    task_seq = task_queue.add_task(TaskType.text2img, {
        'body': params.__dict__})
    if task_seq is None:
        print("[Task Queue] The task queue has reached limit")
        results = [ImageGenerationResult(im=None, seed=0,
                           finish_reason=GenerationFinishReason.queue_is_full)]
        return results

    try:
        waiting_sleep_steps: int = 0
        waiting_start_time = time.perf_counter()
        while not task_queue.is_task_ready_to_start(task_seq):
            if waiting_sleep_steps == 0:
                print(
                    f"[Task Queue] Waiting for task queue become free, seq={task_seq}")
            delay = 0.1
            time.sleep(delay)
            waiting_sleep_steps += 1
            if waiting_sleep_steps % int(10 / delay) == 0:
                waiting_time = time.perf_counter() - waiting_start_time
                print(
                    f"[Task Queue] Already waiting for {waiting_time}S, seq={task_seq}")

        print(f"[Task Queue] Task queue is free, start task, seq={task_seq}")

        task_queue.start_task(task_seq)

        execution_start_time = time.perf_counter()

        # Transform pamameters
        prompt = params.prompt
        negative_prompt = params.negative_prompt
        style_selections = params.style_selections
        performance_selection = params.performance_selection
        aspect_ratios_selection = params.aspect_ratios_selection
        image_number = params.image_number
        image_seed = None if params.image_seed == -1 else params.image_seed
        sharpness = params.sharpness
        guidance_scale = params.guidance_scale
        base_model_name = params.base_model_name
        refiner_model_name = params.refiner_model_name
        loras = params.loras
        input_image_checkbox = params.uov_input_image is not None or params.inpaint_input_image is not None or len(params.image_prompts) > 0
        current_tab = 'uov' if params.uov_method != flags.disabled else 'inpaint' if params.inpaint_input_image is not None else 'ip' if len(params.image_prompts) > 0 else None
        uov_method = params.uov_method
        uov_input_image = params.uov_input_image
        outpaint_selections = params.outpaint_selections
        inpaint_input_image = params.inpaint_input_image

        # Fooocus async_worker.py code start

        outpaint_selections = [o.lower() for o in outpaint_selections]

        loras_user_raw_input = copy.deepcopy(loras)

        raw_style_selections = copy.deepcopy(style_selections)

        uov_method = uov_method.lower()

        if fooocus_expansion in style_selections:
            use_expansion = True
            style_selections.remove(fooocus_expansion)
        else:
            use_expansion = False

        use_style = len(style_selections) > 0
        patch.sharpness = sharpness
        patch.negative_adm = True
        initial_latent = None
        denoising_strength = 1.0
        tiled = False
        inpaint_worker.current_task = None

        if performance_selection == 'Speed':
            steps = 30
            switch = 20
        else:
            steps = 60
            switch = 40

        pipeline.clear_all_caches()  # save memory

        width, height = aspect_ratios[aspect_ratios_selection]

        if input_image_checkbox:
            progressbar(0, 'Image processing ...')
            if current_tab == 'uov' and uov_method != flags.disabled and uov_input_image is not None:
                uov_input_image = HWC3(uov_input_image)
                if 'vary' in uov_method:
                    if not image_is_generated_in_current_ui(uov_input_image, ui_width=width, ui_height=height):
                        uov_input_image = resize_image(uov_input_image, width=width, height=height)
                        print(f'Resolution corrected - users are uploading their own images.')
                    else:
                        print(f'Processing images generated by Fooocus.')
                    if 'subtle' in uov_method:
                        denoising_strength = 0.5
                    if 'strong' in uov_method:
                        denoising_strength = 0.85
                    initial_pixels = core.numpy_to_pytorch(uov_input_image)
                    progressbar(0, 'VAE encoding ...')
                    initial_latent = core.encode_vae(vae=pipeline.xl_base_patched.vae, pixels=initial_pixels)
                    B, C, H, W = initial_latent['samples'].shape
                    width = W * 8
                    height = H * 8
                    print(f'Final resolution is {str((height, width))}.')
                elif 'upscale' in uov_method:
                    H, W, C = uov_input_image.shape
                    progressbar(0, f'Upscaling image from {str((H, W))} ...')

                    uov_input_image = core.numpy_to_pytorch(uov_input_image)
                    uov_input_image = perform_upscale(uov_input_image)
                    uov_input_image = core.pytorch_to_numpy(uov_input_image)[0]
                    print(f'Image upscaled.')

                    if '1.5x' in uov_method:
                        f = 1.5
                    elif '2x' in uov_method:
                        f = 2.0
                    else:
                        f = 1.0

                    width_f = int(width * f)
                    height_f = int(height * f)

                    if image_is_generated_in_current_ui(uov_input_image, ui_width=width_f, ui_height=height_f):
                        uov_input_image = resize_image(uov_input_image, width=int(W * f), height=int(H * f))
                        print(f'Processing images generated by Fooocus.')
                    else:
                        uov_input_image = resize_image(uov_input_image, width=width_f, height=height_f)
                        print(f'Resolution corrected - users are uploading their own images.')

                    H, W, C = uov_input_image.shape
                    image_is_super_large = H * W > 2800 * 2800

                    if 'fast' in uov_method:
                        direct_return = True
                    elif image_is_super_large:
                        print('Image is too large. Directly returned the SR image. '
                              'Usually directly return SR image at 4K resolution '
                              'yields better results than SDXL diffusion.')
                        direct_return = True
                    else:
                        direct_return = False

                    if direct_return:
                        d = [('Upscale (Fast)', '2x')]
                        log(uov_input_image, d, single_line_number=1)
                        outputs.append(['results', [uov_input_image], -1])
                        results = make_results_from_outputs()
                        task_queue.finish_task(task_seq, results, False)
                        return results

                    tiled = True
                    denoising_strength = 1.0 - 0.618
                    steps = int(steps * 0.618)
                    switch = int(steps * 0.67)
                    initial_pixels = core.numpy_to_pytorch(uov_input_image)
                    progressbar(0, 'VAE encoding ...')

                    initial_latent = core.encode_vae(vae=pipeline.xl_base_patched.vae, pixels=initial_pixels, tiled=True)
                    B, C, H, W = initial_latent['samples'].shape
                    width = W * 8
                    height = H * 8
                    print(f'Final resolution is {str((height, width))}.')
            if current_tab == 'inpaint' and isinstance(inpaint_input_image, dict):
                inpaint_image = inpaint_input_image['image']
                inpaint_mask = inpaint_input_image['mask'][:, :, 0]
                if isinstance(inpaint_image, np.ndarray) and isinstance(inpaint_mask, np.ndarray) \
                        and (np.any(inpaint_mask > 127) or len(outpaint_selections) > 0):
                    if len(outpaint_selections) > 0:
                        H, W, C = inpaint_image.shape
                        if 'top' in outpaint_selections:
                            inpaint_image = np.pad(inpaint_image, [[int(H * 0.3), 0], [0, 0], [0, 0]], mode='edge')
                            inpaint_mask = np.pad(inpaint_mask, [[int(H * 0.3), 0], [0, 0]], mode='constant', constant_values=255)
                        if 'bottom' in outpaint_selections:
                            inpaint_image = np.pad(inpaint_image, [[0, int(H * 0.3)], [0, 0], [0, 0]], mode='edge')
                            inpaint_mask = np.pad(inpaint_mask, [[0, int(H * 0.3)], [0, 0]], mode='constant', constant_values=255)

                        H, W, C = inpaint_image.shape
                        if 'left' in outpaint_selections:
                            inpaint_image = np.pad(inpaint_image, [[0, 0], [int(H * 0.3), 0], [0, 0]], mode='edge')
                            inpaint_mask = np.pad(inpaint_mask, [[0, 0], [int(H * 0.3), 0]], mode='constant', constant_values=255)
                        if 'right' in outpaint_selections:
                            inpaint_image = np.pad(inpaint_image, [[0, 0], [0, int(H * 0.3)], [0, 0]], mode='edge')
                            inpaint_mask = np.pad(inpaint_mask, [[0, 0], [0, int(H * 0.3)]], mode='constant', constant_values=255)

                        inpaint_image = np.ascontiguousarray(inpaint_image.copy())
                        inpaint_mask = np.ascontiguousarray(inpaint_mask.copy())

                    inpaint_worker.current_task = inpaint_worker.InpaintWorker(image=inpaint_image, mask=inpaint_mask,
                                                                               is_outpaint=len(outpaint_selections) > 0)

                    # print(f'Inpaint task: {str((height, width))}')
                    # outputs.append(['results', inpaint_worker.current_task.visualize_mask_processing()])
                    # return

                    progressbar(0, 'Downloading inpainter ...')
                    inpaint_head_model_path, inpaint_patch_model_path = path.downloading_inpaint_models()
                    loras += [(inpaint_patch_model_path, 1.0)]

                    inpaint_pixels = core.numpy_to_pytorch(inpaint_worker.current_task.image_ready)
                    progressbar(0, 'VAE encoding ...')
                    initial_latent = core.encode_vae(vae=pipeline.xl_base_patched.vae, pixels=inpaint_pixels)
                    inpaint_latent = initial_latent['samples']
                    B, C, H, W = inpaint_latent.shape
                    inpaint_mask = core.numpy_to_pytorch(inpaint_worker.current_task.mask_ready[None])
                    inpaint_mask = torch.nn.functional.avg_pool2d(inpaint_mask, (8, 8))
                    inpaint_mask = torch.nn.functional.interpolate(inpaint_mask, (H, W), mode='bilinear')
                    inpaint_worker.current_task.load_latent(latent=inpaint_latent, mask=inpaint_mask)

                    progressbar(0, 'VAE inpaint encoding ...')

                    inpaint_mask = (inpaint_worker.current_task.mask_ready > 0).astype(np.float32)
                    inpaint_mask = torch.tensor(inpaint_mask).float()

                    vae_dict = core.encode_vae_inpaint(
                        mask=inpaint_mask, vae=pipeline.xl_base_patched.vae, pixels=inpaint_pixels)

                    inpaint_latent = vae_dict['samples']
                    inpaint_mask = vae_dict['noise_mask']
                    inpaint_worker.current_task.load_inpaint_guidance(latent=inpaint_latent, mask=inpaint_mask, model_path=inpaint_head_model_path)

                    B, C, H, W = inpaint_latent.shape
                    height, width = inpaint_worker.current_task.image_raw.shape[:2]
                    print(f'Final resolution is {str((height, width))}, latent is {str((H * 8, W * 8))}.')

        progressbar(1, 'Initializing ...')

        raw_prompt = prompt
        raw_negative_prompt = negative_prompt

        prompts = remove_empty_str([safe_str(p) for p in prompt.split('\n')], default='')
        negative_prompts = remove_empty_str([safe_str(p) for p in negative_prompt.split('\n')], default='')

        prompt = prompts[0]
        negative_prompt = negative_prompts[0]

        extra_positive_prompts = prompts[1:] if len(prompts) > 1 else []
        extra_negative_prompts = negative_prompts[1:] if len(negative_prompts) > 1 else []

        seed = image_seed
        max_seed = int(1024 * 1024 * 1024)
        if not isinstance(seed, int):
            seed = random.randint(1, max_seed)
        if seed < 0:
            seed = - seed
        seed = seed % max_seed

        progressbar(3, 'Loading models ...')

        pipeline.refresh_everything(
            refiner_model_name=refiner_model_name,
            base_model_name=base_model_name,
            loras=loras)

        progressbar(3, 'Processing prompts ...')

        positive_basic_workloads = []
        negative_basic_workloads = []

        if use_style:
            for s in style_selections:
                p, n = apply_style(s, positive=prompt)
                positive_basic_workloads.append(p)
                negative_basic_workloads.append(n)
        else:
            positive_basic_workloads.append(prompt)

        negative_basic_workloads.append(negative_prompt)  # Always use independent workload for negative.

        positive_basic_workloads = positive_basic_workloads + extra_positive_prompts
        negative_basic_workloads = negative_basic_workloads + extra_negative_prompts

        positive_basic_workloads = remove_empty_str(positive_basic_workloads, default=prompt)
        negative_basic_workloads = remove_empty_str(negative_basic_workloads, default=negative_prompt)

        positive_top_k = len(positive_basic_workloads)
        negative_top_k = len(negative_basic_workloads)

        tasks = [dict(
            task_seed=seed + i,
            positive=positive_basic_workloads,
            negative=negative_basic_workloads,
            expansion='',
            c=[None, None],
            uc=[None, None],
        ) for i in range(image_number)]

        if use_expansion:
            for i, t in enumerate(tasks):
                progressbar(5, f'Preparing Fooocus text #{i + 1} ...')
                expansion = pipeline.expansion(prompt, t['task_seed'])
                print(f'[Prompt Expansion] New suffix: {expansion}')
                t['expansion'] = expansion
                t['positive'] = copy.deepcopy(t['positive']) + [join_prompts(prompt, expansion)]  # Deep copy.

        for i, t in enumerate(tasks):
            progressbar(7, f'Encoding base positive #{i + 1} ...')
            t['c'][0] = pipeline.clip_encode(sd=pipeline.xl_base_patched, texts=t['positive'],
                                             pool_top_k=positive_top_k)

        for i, t in enumerate(tasks):
            progressbar(9, f'Encoding base negative #{i + 1} ...')
            t['uc'][0] = pipeline.clip_encode(sd=pipeline.xl_base_patched, texts=t['negative'],
                                              pool_top_k=negative_top_k)

        if pipeline.xl_refiner is not None:
            virtual_memory.load_from_virtual_memory(pipeline.xl_refiner.clip.cond_stage_model)

            for i, t in enumerate(tasks):
                progressbar(11, f'Encoding refiner positive #{i + 1} ...')
                t['c'][1] = pipeline.clip_encode(sd=pipeline.xl_refiner, texts=t['positive'],
                                                 pool_top_k=positive_top_k)

            for i, t in enumerate(tasks):
                progressbar(13, f'Encoding refiner negative #{i + 1} ...')
                t['uc'][1] = pipeline.clip_encode(sd=pipeline.xl_refiner, texts=t['negative'],
                                                  pool_top_k=negative_top_k)

            virtual_memory.try_move_to_virtual_memory(pipeline.xl_refiner.clip.cond_stage_model)

        results = []
        all_steps = steps * image_number

        def callback(step, x0, x, total_steps, y):
            done_steps = current_task_id * steps + step
            outputs.append(['preview', (
                int(15.0 + 85.0 * float(done_steps) / float(all_steps)),
                f'Step {step}/{total_steps} in the {current_task_id + 1}-th Sampling',
                y)])

        print(f'[ADM] Negative ADM = {patch.negative_adm}')

        outputs.append(['preview', (13, 'Starting tasks ...', None)])
        for current_task_id, task in enumerate(tasks):
            try:
                execution_start_time = time.perf_counter()

                imgs = pipeline.process_diffusion(
                    positive_cond=task['c'],
                    negative_cond=task['uc'],
                    steps=steps,
                    switch=switch,
                    width=width,
                    height=height,
                    image_seed=task['task_seed'],
                    callback=callback,
                    latent=initial_latent,
                    denoise=denoising_strength,
                    tiled=tiled
                )

                if inpaint_worker.current_task is not None:
                    imgs = [inpaint_worker.current_task.post_process(x) for x in imgs]

                execution_time = time.perf_counter() - execution_start_time
                print(f'Diffusion time: {execution_time:.2f} seconds')

                for x in imgs:
                    d = [
                        ('Prompt', raw_prompt),
                        ('Negative Prompt', raw_negative_prompt),
                        ('Fooocus V2 Expansion', task['expansion']),
                        ('Styles', str(raw_style_selections)),
                        ('Performance', performance_selection),
                        ('Resolution', str((width, height))),
                        ('Sharpness', sharpness),
                        ('Base Model', base_model_name),
                        ('Refiner Model', refiner_model_name),
                        ('Seed', task['task_seed'])
                    ]
                    for n, w in loras_user_raw_input:
                        if n != 'None':
                            d.append((f'LoRA [{n}] weight', w))
                    log(x, d, single_line_number=3)

                # Fooocus async_worker.py code end

                results.append(ImageGenerationResult(
                    im=imgs[0], seed=task['task_seed'], finish_reason=GenerationFinishReason.success))
            except model_management.InterruptProcessingException as e:
                print('User stopped')
                results.append(ImageGenerationResult(
                        im=None, seed=task['task_seed'], finish_reason=GenerationFinishReason.user_cancel))
                break
            except Exception as e:
                print('Process failed:', e)
                results.append(ImageGenerationResult(
                    im=None, seed=task['task_seed'], finish_reason=GenerationFinishReason.error))

            execution_time = time.perf_counter() - execution_start_time
            print(f'Generating and saving time: {execution_time:.2f} seconds')

        print(f"[Task Queue] Finish task, seq={task_seq}")
        task_queue.finish_task(task_seq, results, False)

        return results
    except Exception as e:
        print('Worker error:', e)
        print(f"[Task Queue] Finish task, seq={task_seq}")
        task_queue.finish_task(task_seq, [], True)
        raise e
