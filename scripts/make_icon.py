# -*- coding: utf-8 -*-
"""用 PIL 画小工具箱应用图标并输出多尺寸 .ico"""
from PIL import Image, ImageDraw

S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# ---- 圆角方形背景（垂直渐变蓝）----
radius = 110
top_color = (24, 95, 165)     # #185fa5
bottom_color = (12, 62, 112)  # 深一档
bg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
bgd = ImageDraw.Draw(bg)
for y in range(S):
    t = y / S
    c = tuple(int(top_color[i] + (bottom_color[i] - top_color[i]) * t) for i in range(3)) + (255,)
    bgd.line([(0, y), (S, y)], fill=c)

# 圆角蒙版
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S, S], radius=radius, fill=255)
img.paste(bg, (0, 0), mask)

# ---- 白色工具箱图形 ----
W = (255, 255, 255, 255)
W_SOFT = (255, 255, 255, 200)

# 提手（圆弧）
d.arc([186, 106, 326, 226], start=180, end=360, fill=W, width=26)
# 箱盖
d.rounded_rectangle([96, 200, 416, 262], radius=18, fill=W)
# 箱体
d.rounded_rectangle([76, 270, 436, 424], radius=22, fill=W)
# 箱体上的分割线（用背景蓝挖出）
d.line([(76, 300), (436, 300)], fill=(24, 95, 165, 255), width=10)
# 锁扣
d.rounded_rectangle([226, 258, 286, 322], radius=10, fill=(24, 95, 165, 255))
d.rounded_rectangle([244, 274, 268, 306], radius=6, fill=W)

# 输出多尺寸 ico
ico_path = r"D:\workspace\WorkBuddy\toolbox\app\icon.ico"
img.save(ico_path, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.save(r"D:\workspace\WorkBuddy\toolbox\app\icon.png")
import os
print("图标已生成:", ico_path, os.path.getsize(ico_path), "字节")
