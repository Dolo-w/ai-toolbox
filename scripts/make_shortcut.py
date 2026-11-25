# -*- coding: utf-8 -*-
"""创建开始菜单快捷方式：小工具箱"""
import os
import win32com.client

start_menu = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs")
lnk_path = os.path.join(start_menu, "小工具箱.lnk")

shell = win32com.client.Dispatch("WScript.Shell")
lnk = shell.CreateShortCut(lnk_path)
lnk.TargetPath = r"D:\workspace\WorkBuddy\toolbox\portable\AIToolbox\xiaoGongJuXiang.exe"
lnk.Arguments = ""
lnk.WorkingDirectory = r"D:\workspace\WorkBuddy\toolbox\portable\AIToolbox"
lnk.Description = "本机 AI 小工具箱：证件照/字幕/消除笔/老照片修复"
lnk.IconLocation = r"D:\workspace\WorkBuddy\toolbox\portable\AIToolbox\backend\icon.ico,0"
lnk.Save()
print("快捷方式已创建:", lnk_path)
print("存在:", os.path.exists(lnk_path))
