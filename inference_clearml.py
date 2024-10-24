import os
import argparse
import logging
import math
from omegaconf import OmegaConf
from datetime import datetime
from pathlib import Path
from clearml import Dataset, Task, Logger
import numpy as np
import torch.jit
from torchvision.datasets.folder import pil_loader
from torchvision.transforms.functional import pil_to_tensor, resize, center_crop
from torchvision.transforms.functional import to_pil_image


from mimicmotion.utils.geglu_patch import patch_geglu_inplace
patch_geglu_inplace()

from constants import ASPECT_RATIO

from mimicmotion.pipelines.pipeline_mimicmotion import MimicMotionPipeline
from mimicmotion.utils.loader import create_pipeline
from mimicmotion.utils.utils import save_to_mp4
from mimicmotion.dwpose.preprocess import get_video_pose, get_image_pose
from dotenv import load_dotenv
import yaml



load_dotenv()

def set_up_media_logging():
    logger = Logger.current_logger()
    logger.set_default_upload_destination(uri=f"s3://sil-mimicmotion")
    return logger

def get_clearml_paths():

    # create outputs folder
    os.makedirs("outputs", exist_ok=True)
    # Getting vits path
    curr_dir = os.getcwd().split('/')
    print("Current Directory: ", curr_dir)
    mimic_path = '/'.join(curr_dir)

    dataset = Dataset.get(dataset_id="47cf215eb8e54f099b21cc2d17f3460d")
    models_path = dataset.get_mutable_local_copy(
        target_folder="./",
        overwrite=True
    )

    # Imprimir el contenido del directorio
    print("Models path content:", os.listdir(models_path))

    # # Load the YAML file
    # with open('configs/test.yaml', 'r') as file:
    #     config = yaml.safe_load(file)

    # # Modify the YAML content
    # config['ckpt_path'] = f'{path_dw + "/MimicMotion_1-1.pth"}'

    # # Save the updated YAML file
    # with open('configs/test.yaml', 'w') as file:
    #     yaml.dump(config, file)

    return mimic_path

task = Task.init(project_name="MimicMotion", task_name="Inference v3")
aws_region = os.getenv('AWS_REGION')
aws_access_key_id = os.getenv('AWS_ACCESS_KEY_ID')
aws_secret_access_key = os.getenv('AWS_SECRET_ACCESS_KEY')
token = os.getenv('HUGGINGFACE_TOKEN')
task.set_base_docker(
                    docker_image="alejandroquinterosil/clearml-image:mimicmotion",
                    docker_arguments=[
                        f"--env AWS_REGION={aws_region}",
                        f"--env AWS_ACCESS_KEY_ID={aws_access_key_id}",
                        f"--env AWS_SECRET_ACCESS_KEY={aws_secret_access_key}",
                        f"--env HF_TOKEN={token}"],
                    )
task.execute_remotely(queue_name="jobs_urgent", exit_process=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s: [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")



media_logger = set_up_media_logging()
mimic_path = get_clearml_paths()

def preprocess(video_path, image_path, resolution=576, sample_stride=2):
    """preprocess ref image pose and video pose

    Args:
        video_path (str): input video pose path
        image_path (str): reference image path
        resolution (int, optional):  Defaults to 576.
        sample_stride (int, optional): Defaults to 2.
    """
    image_pixels = pil_loader(image_path)
    image_pixels = pil_to_tensor(image_pixels) # (c, h, w)
    h, w = image_pixels.shape[-2:]
    ############################ compute target h/w according to original aspect ratio ###############################
    if h>w:
        w_target, h_target = resolution, int(resolution / ASPECT_RATIO // 64) * 64
    else:
        w_target, h_target = int(resolution / ASPECT_RATIO // 64) * 64, resolution
    h_w_ratio = float(h) / float(w)
    if h_w_ratio < h_target / w_target:
        h_resize, w_resize = h_target, math.ceil(h_target / h_w_ratio)
    else:
        h_resize, w_resize = math.ceil(w_target * h_w_ratio), w_target
    image_pixels = resize(image_pixels, [h_resize, w_resize], antialias=None)
    image_pixels = center_crop(image_pixels, [h_target, w_target])
    image_pixels = image_pixels.permute((1, 2, 0)).numpy()
    ##################################### get image&video pose value #################################################
    image_pose = get_image_pose(image_pixels)
    video_pose = get_video_pose(video_path, image_pixels, sample_stride=sample_stride)
    pose_pixels = np.concatenate([np.expand_dims(image_pose, 0), video_pose])
    image_pixels = np.transpose(np.expand_dims(image_pixels, 0), (0, 3, 1, 2))
    return torch.from_numpy(pose_pixels.copy()) / 127.5 - 1, torch.from_numpy(image_pixels) / 127.5 - 1


def run_pipeline(pipeline: MimicMotionPipeline, image_pixels, pose_pixels, device, task_config):
    image_pixels = [to_pil_image(img.to(torch.uint8)) for img in (image_pixels + 1.0) * 127.5]
    generator = torch.Generator(device=device)
    generator.manual_seed(task_config.seed)
    frames = pipeline(
        image_pixels, image_pose=pose_pixels, num_frames=pose_pixels.size(0),
        tile_size=task_config.num_frames, tile_overlap=task_config.frames_overlap,
        height=pose_pixels.shape[-2], width=pose_pixels.shape[-1], fps=7,
        noise_aug_strength=task_config.noise_aug_strength, num_inference_steps=task_config.num_inference_steps,
        generator=generator, min_guidance_scale=task_config.guidance_scale,
        max_guidance_scale=task_config.guidance_scale, decode_chunk_size=8, output_type="pt", device=device
    ).frames.cpu()
    video_frames = (frames * 255.0).to(torch.uint8)

    for vid_idx in range(video_frames.shape[0]):
        # deprecated first frame because of ref image
        _video_frames = video_frames[vid_idx, 1:]

    return _video_frames


@torch.no_grad()
def main(args):
    if not args.no_use_float16 :
        torch.set_default_dtype(torch.float16)

    infer_config = OmegaConf.load(args.inference_config)
    pipeline = create_pipeline(infer_config, device)

    for task in infer_config.test_case:
        ############################################## Pre-process data ##############################################
        pose_pixels, image_pixels = preprocess(
            task.ref_video_path, task.ref_image_path,
            resolution=task.resolution, sample_stride=task.sample_stride
        )
        ########################################### Run MimicMotion pipeline ###########################################
        _video_frames = run_pipeline(
            pipeline,
            image_pixels, pose_pixels,
            device, task
        )
        ################################### save results to output folder. ###########################################
        save_to_mp4(
            _video_frames,
            f"{args.output_dir}/{os.path.basename(task.ref_video_path).split('.')[0]}" \
            f"_{datetime.now().strftime('%Y%m%d%H%M%S')}.mp4",
            fps=task.fps,
        )

        media_logger.report_media(
            media_path=f"{args.output_dir}/{os.path.basename(task.ref_video_path).split('.')[0]}" \
            f"_{datetime.now().strftime('%Y%m%d%H%M%S')}.mp4",
            title=f"{os.path.basename(task.ref_video_path).split('.')[0]}",
            iteration=task.id
        )

def set_logger(log_file=None, log_level=logging.INFO):
    log_handler = logging.FileHandler(log_file, "w")
    log_handler.setFormatter(
        logging.Formatter("[%(asctime)s][%(name)s][%(levelname)s]: %(message)s")
    )
    log_handler.setLevel(log_level)
    logger.addHandler(log_handler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--log_file", type=str, default=None)
    parser.add_argument("--inference_config", type=str, default="configs/test.yaml") #ToDo
    parser.add_argument("--output_dir", type=str, default="outputs/", help="path to output")
    parser.add_argument("--no_use_float16",
                        action="store_true",
                        help="Whether use float16 to speed up inference",
    )
    args = parser.parse_args()
    print("---------------------------------------------------------------")
    print("Args: ", args)
    print("---------------------------------------------------------------")
    print(mimic_path)
    Path(mimic_path + args.output_dir).mkdir(parents=False, exist_ok=True)
    set_logger(args.log_file \
               if args.log_file is not None else f"{args.output_dir}/{datetime.now().strftime('%Y%m%d%H%M%S')}.log")
    main(args)
    logger.info(f"--- Finished ---")

