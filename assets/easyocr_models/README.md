# EasyOCR 离线模型

本目录包含项目运行英文疲劳值 OCR 所需的两份模型：

- `craft_mlt_25k.pth`
- `english_g2.pth`

文件校验值与 EasyOCR 1.7.2 配置中的英文模型校验值一致。项目启动时会校验模型，
并以 `download_enabled=False` 加载它们，因此新电脑运行完整项目文件夹时不需要访问
GitHub Releases 下载模型。
