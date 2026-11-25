# 第三方组件与协议

本项目（`app/`、`backend/deploy_api.py`、`scripts/`）代码使用 [MIT](LICENSE)。

以下为运行时依赖与它们各自的协议。**分发便携版时请一并保留这些声明**。

| 组件 | 用途 | 协议 | 是否在本仓库内 |
|---|---|---|---|
| [HivisionIDPhotos](https://github.com/Zeyi-Lin/HivisionIDPhotos) | 证件照抠图与排版 | Apache-2.0 | 否（需自行安装） |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | 语音转写兜底 | MIT | 否（pip 安装） |
| [SenseVoice](https://github.com/FunAudioLLM/SenseVoice) + [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | 中文语音转写 | Apache-2.0 | 模型不在仓库内 |
| EDSR / FSRCNN 超分模型 | 老照片放大 | Apache-2.0 | 否（需下载） |
| [OpenCV](https://opencv.org/) | 消除笔 inpaint、图像处理 | Apache-2.0 | 否（pip 安装） |
| [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) | 提供 ffmpeg 可执行文件 | BSD-2-Clause | 否（pip 安装） |
| [HarmonyOS Sans](https://developer.harmonyos.com/cn/docs/design/font-0000001157868583) | 界面与字幕字体 | HarmonyOS Sans 字体协议 | 否（需下载） |

## 关于字体

HarmonyOS Sans 允许个人与商业使用，但**不可单独售卖字体本身**。
本仓库不含该字体文件，请到华为开发者网站下载后放入 `app/fonts/`。

## 关于模型

仓库里没有 `.onnx` / `.pb` 模型文件，原因有两个：体积（合计约 280 MB 以上）与协议归属。
缺失时程序不会崩：

- 语音：SenseVoice 缺失 → 回退 faster-whisper；都缺失 → 字幕功能报错，其他功能正常。
- 超分：模型缺失 → 回退 Lanczos 插值 + 锐化，接口返回 `engine: "LanczosFallback"`。
- 证件照：HivisionIDPhotos 未安装 → 只有该服务起不来，8081 上的四个功能不受影响。
