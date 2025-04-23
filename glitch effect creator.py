#!/usr/bin/env python3
from PIL import Image
import numpy as np
import imageio
import random
import os

# ——— CONFIG ———
input_path = 'ChatGPT Image Apr 23, 2025, 03_21_32 AM.png'   # your static banner file
output_gif  = 'BUTCH3R_glitch1.gif'
output_mp4  = 'BUTCH3R_glitch1.mp4'
fps         = 20
duration_s  = 3
max_shift   = 0.03   # fraction of width for glitch shift
# ————————

# Load base image
banner = Image.open(input_path).convert('RGB')
arr = np.array(banner)
h, w, _ = arr.shape
num_frames = fps * duration_s

def glitch_frame(src):
    img = src.copy()
    # Random horizontal band shifts
    for _ in range(random.randint(3, 7)):
        y       = random.randint(0, h-1)
        band_h  = random.randint(1, int(h * 0.05))
        shift   = random.randint(int(-w*max_shift), int(w*max_shift))
        patch   = img[y:y+band_h].copy()
        img[y:y+band_h] = 0
        if shift > 0:
            img[y:y+band_h, shift:] = patch[:, :-shift]
        elif shift < 0:
            img[y:y+band_h, :w+shift] = patch[:, -shift:]
    # Scan-line effect
    img[::2] = (img[::2] * 0.7).astype(np.uint8)
    return img

# Generate all frames
frames = [glitch_frame(arr) for _ in range(num_frames)]

# Save GIF
imageio.mimsave(output_gif, frames, fps=fps, loop=0)

# Save MP4 (requires ffmpeg)
writer = imageio.get_writer(output_mp4, fps=fps, codec='libx264')
for f in frames:
    writer.append_data(f)
writer.close()

print(f"✔️  Exported:\n  • GIF → {os.path.abspath(output_gif)}\n  • MP4 → {os.path.abspath(output_mp4)}")
