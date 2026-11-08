# smart-panel

由 AI 根据以下需求生成：

> 为智能中控屏设备开发固件：设备上电后进入主界面；支持 Wi-Fi 配网并连接云端；支持 OTA 远程升级并生效；断网后能自动重连。需提供可在本地运行的模拟实现，输出串口日志与最终状态，供自动化测试驱动。

## 运行

```bash
python main.py boot
python main.py provision
python main.py ota
python main.py reconnect
```

每个场景输出串口日志，最后一行是 `STATE {...}` 的结构化设备状态。
