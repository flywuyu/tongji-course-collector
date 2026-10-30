# 第三方组件

本仓库的 MIT 许可适用于项目自身代码。依赖由使用者单独安装，保持各自的许可证和通知；仓库不包含它们的二进制、模型权重或浏览器安装包。

| 组件 | 用途 | 官方来源/许可 |
|---|---|---|
| OpenAI Whisper | 本地语音识别和模型定义 | [代码与许可证](https://github.com/openai/whisper)；MIT |
| PyTorch | 本地张量计算/CUDA | [官方项目](https://github.com/pytorch/pytorch)；BSD 风格及其第三方通知 |
| Playwright | 本人浏览器登录与页面访问 | [官方项目](https://github.com/microsoft/playwright-python)；Apache-2.0 |
| RapidOCR | CPU OCR | [官方项目](https://github.com/RapidAI/RapidOCR)；Apache-2.0，模型另按其通知 |
| ONNX Runtime | OCR 推理 | [官方项目](https://github.com/microsoft/onnxruntime)；MIT |
| ReportLab | 逐页生成图片 PDF | [官方来源](https://www.reportlab.com/opensource/)；BSD 风格 |
| pypdf | PDF 页数核验 | [官方许可证](https://github.com/py-pdf/pypdf/blob/main/LICENSE)；BSD |
| NumPy / Pillow / psutil | 数值、图像和进程管理 | 各项目的 BSD / HPND 等许可证 |
| FFmpeg | 媒体探测、下载和抽帧 | [官方许可说明](https://ffmpeg.org/legal.html)；取决于使用者安装的构建 |

公开版 PDF 使用 ReportLab 与 pypdf，不分发原型环境的 PDF 库、FFmpeg 或模型。安装依赖时请保留上游随包提供的许可文件。
