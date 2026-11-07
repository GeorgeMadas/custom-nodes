"""
Aiorbust Temporal Mask Smooth - ComfyUI custom node.

Stabilizes a MASK batch coming out of SAM3 video propagation before it is used
to composite or condition a video edit. Four stages, in order:

  1. temporal median  - kills single-frame masks that pop in from nowhere
  2. closing          - fills pinholes and reconnects broken edges
  3. dilate           - pushes the boundary outside the subject
  4. gaussian blur    - soft edge, so no hard seam survives the re-encode
"""

import torch
import torch.nn.functional as F


def _temporal_median(masks: torch.Tensor, radius: int, chunk_size: int = 32) -> torch.Tensor:
    """
    Median filter along the frame axis. masks: [B, H, W].
    Edges are handled by clamping indices, so the first and last frames
    replicate rather than fade out.
    Processed in chunks - a full [B, window, H, W] stack blows up VRAM on long clips.
    """
    if radius < 1:
        return masks

    frame_count = masks.shape[0]
    window = torch.arange(-radius, radius + 1, device=masks.device)
    output = torch.empty_like(masks)

    for start in range(0, frame_count, chunk_size):
        end = min(start + chunk_size, frame_count)
        centers = torch.arange(start, end, device=masks.device).unsqueeze(1)
        indices = (centers + window.unsqueeze(0)).clamp(0, frame_count - 1)
        stack = masks[indices]                      # [chunk, window, H, W]
        output[start:end] = stack.median(dim=1).values

    return output


def _dilate(masks: torch.Tensor, radius: int) -> torch.Tensor:
    if radius < 1:
        return masks
    size = 2 * radius + 1
    return F.max_pool2d(masks, kernel_size=size, stride=1, padding=radius)


def _erode(masks: torch.Tensor, radius: int) -> torch.Tensor:
    if radius < 1:
        return masks
    return -_dilate(-masks, radius)


def _gaussian_blur(masks: torch.Tensor, radius: int) -> torch.Tensor:
    """Separable gaussian. sigma derived from radius so one knob controls both."""
    if radius < 1:
        return masks

    size = 2 * radius + 1
    sigma = radius / 2.0
    coords = torch.arange(size, dtype=masks.dtype, device=masks.device) - radius
    kernel = torch.exp(-(coords ** 2) / (2 * sigma ** 2))
    kernel = kernel / kernel.sum()

    blurred = F.conv2d(
        F.pad(masks, (radius, radius, 0, 0), mode="replicate"),
        kernel.view(1, 1, 1, size),
    )
    blurred = F.conv2d(
        F.pad(blurred, (0, 0, radius, radius), mode="replicate"),
        kernel.view(1, 1, size, 1),
    )
    return blurred


class AiorbustTemporalMaskSmooth:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "masks": ("MASK",),
                "temporal_radius": ("INT", {"default": 2, "min": 0, "max": 15}),
                "closing_radius": ("INT", {"default": 3, "min": 0, "max": 64}),
                "dilate_radius": ("INT", {"default": 10, "min": 0, "max": 128}),
                "blur_radius": ("INT", {"default": 5, "min": 0, "max": 128}),
                "threshold": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.01}),
            }
        }

    RETURN_TYPES = ("MASK",)
    FUNCTION = "process"
    CATEGORY = "Aiorbust"
    DESCRIPTION = ("Stabilizes a MASK batch from SAM3 video propagation: temporal median, "
                   "closing, dilate, gaussian blur — in that order.")

    def process(self, masks, temporal_radius, closing_radius, dilate_radius, blur_radius, threshold):
        if masks.dim() == 2:
            masks = masks.unsqueeze(0)

        working = masks.float()

        # Binarize first. SAM3 confidence gradients make the median unstable,
        # and morphology on soft values smears the boundary.
        working = (working > threshold).float()

        working = _temporal_median(working, temporal_radius)

        # Closing = dilate then erode. Fills holes without growing the shape.
        working = _erode(_dilate(working.unsqueeze(1), closing_radius), closing_radius).squeeze(1)

        working = _dilate(working.unsqueeze(1), dilate_radius).squeeze(1)
        working = _gaussian_blur(working.unsqueeze(1), blur_radius).squeeze(1)

        return (working.clamp(0.0, 1.0),)


NODE_CLASS_MAPPINGS = {"AiorbustTemporalMaskSmooth": AiorbustTemporalMaskSmooth}
NODE_DISPLAY_NAME_MAPPINGS = {"AiorbustTemporalMaskSmooth": "Aiorbust Temporal Mask Smooth"}
