# ReportPdfWorker 打包与部署

把 PDF 外部转换 Worker（`report_pdf_worker.py`）打包为免 Python 环境的单文件 exe，
部署到任意装有 **WPS 或 MS Office** 的 Windows 常开机器上。

## 一、打包（在开发机执行一次）

```powershell
# 依赖：Python 3.8+，安装打包工具与 COM 支持
pip install pyinstaller pywin32

# 打包（单文件 exe，内嵌 Python 运行时）
cd tools\pdf_worker
python -m PyInstaller --onefile --console --name ReportPdfWorker `
  --hidden-import win32timezone report_pdf_worker.py
```

产物：`tools/pdf_worker/dist/ReportPdfWorker.exe`（约 12MB，32/64 位随构建机 Python 位数）。
`build/`、`*.spec` 为中间产物，已加入 .gitignore。

## 二、部署（目标 Windows 机器）

1. 拷贝两个文件到任意目录（如 `C:\ReportPdfWorker\`）：
   - `ReportPdfWorker.exe`
   - `worker_config.example.json` → 重命名为 `worker_config.json` 并填写：
     ```json
     {
       "server_url": "http://10.1.1.4:8000",
       "api_key": "与服务端 V1_API_KEY 一致",
       "poll_interval": 10,
       "work_dir": ""
     }
     ```
2. 双击运行（或注册为计划任务/服务常驻）。首次启动若无配置文件会自动生成默认模板。
3. 前提：机器已安装 **WPS 表格** 或 **MS Excel**（COM 转换依赖，无法打进 exe）。
4. 服务端确认 `.env` 中 `REPORT_PDF_MODE=external`。

## 三、运行逻辑

轮询间隔领取任务（X-API-Key 鉴权）→ 从 MinIO 预签名 URL 拉 xlsx →
COM 转 PDF → multipart 回传服务端 → 服务端归档 MinIO 并触发 MES 上传。
失败自动回报错误，任务可在报告页面重试。

## 四、常见问题

| 现象 | 处理 |
|---|---|
| 启动即退出 | 查看 worker_config.json 是否合法 JSON |
| claim failed: 401 | api_key 与服务端 V1_API_KEY 不一致 |
| 转换失败 WPS | 确认装了 WPS 表格或 MS Excel；首次 COM 调用可能需手动打开一次 WPS |
| COM convert timeout | 转换超过 `convert_timeout`（默认 120s）会强杀 COM 宿主并回报失败；若常超时，调大该值或确认 WPS 无弹窗 |
| 杀毒软件报毒 | PyInstaller 单文件常见误报；可改 `--onedir` 打包或对 exe 做数字签名 |
