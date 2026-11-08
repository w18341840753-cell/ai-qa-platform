# AI-Native QA Platform

面向**嵌入式 / IoT 设备**的 AI 原生自动化测试平台。

一句话：**从一句需求开始，AI 写出产品代码 → 打包 → 烧到设备 → 自己设计测试 → 执行 → 判定 → 出报告 → 提 bug → 确认闭环。**

```
需求描述
   ↓  ★ AI 生成产品代码（被测试对象也是 AI 写的，生成后立刻自检 + 自修复）
构建打包
   ↓
部署到设备（烧录 / 安装 / 解包）
   ↓  ★ AI 设计测试计划（读代码变更，判断风险面）
执行（真实驱动设备，抓串口日志 + 屏幕状态）
   ↓  ★ AI 判定（日志硬信号 → 设备状态契约 → 视觉模型，三级证据）
报告
   ↓  ★ 自动建 bug（按证据键去重，不重复报同一问题）
人工确认闭环
```

★ 标记的四层是自研核心；构建、部署、执行全部复用成熟组件。

---

## 快速开始（无需 API Key、无需真实设备）

```bash
git clone <this-repo> && cd ai-qa-platform

# 一键演示完整 AI 原生链路
python -m aqa demo
```

实际输出（真实执行，不是模拟结果）：

```
[1/10] AI 生成产品代码 ✅ files=['README.md', 'main.py', 'panel/__init__.py', 'panel/device.py']
[2/10] 触发       ✅
[3/10] 构建       ✅ files=['main.py', 'panel/device.py', ...]
[4/10] 部署到设备   ✅ artifact=runs/.../firmware.zip
[5/10] AI 设计测试  ✅ 4 个用例
[6/10] 执行       ✅ total=4
[7/10] AI 判定     ✅ PASS 2 / FAIL 2
[8/10] 出报告      ✅ 通过率 50.0%
[9/10] 自动建 bug  ✅ filed=2
[10/10] 确认闭环    ✅ confirmed=2
```

用真模型跑（`--model-mode api`）时用例数量由模型决定，通常 7~11 条，
报告里会多出「状态契约」「模型用量（tokens / 元）」「无法判定 / 未执行」三行。

生成的缺陷（由 AI 撰写，含复现步骤）：

- `LOCAL-001` 【配网回归】配网成功但设备反复重启，云端始终离线
- `LOCAL-002` 【OTA 回归】升级成功但版本号仍为旧固件，疑似 boot 分区未切换

> **为什么这不是「剧本」**：模拟设备会真的把产物解包、启动产品进程、抓取它打印的串口日志和
> `STATE` 行，判定层读的是这些真实输出。产品代码里的两处缺陷（配网 ACK 超时重置、OTA 未切
> 换 boot 分区）确实由执行过程暴露，不是预先写死的 PASS/FAIL。

## 只测已有代码

```bash
python -m aqa run --mock --event examples/push_event.json
```

## 接入真实模型（一个 DeepSeek Key 就够）

```bash
cp .env.example .env       # 填 AQA_TEXT_API_KEY 即可，视觉通道默认复用它
python -m aqa demo --model-mode api
```

模型分工：

| 角色 | 默认模型 | 干什么 |
|---|---|---|
| 文本推理 | **deepseek-flash** | 生成产品代码、设计测试用例、失败归因、撰写 bug |
| 视觉理解 | **deepseek-flash**（同一个 Key） | 看设备截图，判断界面是否异常 |

> 实测 `deepseek-flash` 支持图像输入，所以不用再单独申请视觉模型的 Key。
> 想换 Qwen-VL / 本地 VLM，只改 `AQA_VISION_BASE_URL` / `AQA_VISION_MODEL` 三个变量即可。

**推理模型的 token 预算（踩过的坑）**：`deepseek-flash` 会先把 token 花在思考上，
写代码这种长输出任务给 8k 会直接被思考吃光、返回空内容。所以平台分档设置预算：

| 变量 | 默认 | 用途 |
|---|---|---|
| `AQA_MAX_TOKENS` | 8192 | 设计测试、归因、写 bug |
| `AQA_MAX_TOKENS_GEN` | 32768 | 生成产品代码（每文件一次调用，截断会自动翻倍重试） |
| `AQA_MAX_TOKENS_VISION` | 2048 | 视觉判定 |

一次完整 API 演示（10 个用例）约消耗 12 万 token、**0.3~0.5 元**，报告里会打印实测用量。

### 演示「有缺陷的版本」

```bash
python -m aqa demo --model-mode api --buggy      # 让 AI 在生成的产品里保留两处真实缺陷
```

## 接入真实设备

改环境变量即可切换适配器，代码不用动：

```bash
# Android 底座设备（车机、智能屏、机顶盒）
AQA_DEVICE_KIND=adb AQA_DEVICE_ID=emulator-5554

# MCU / 开发板（串口烧录）
AQA_DEVICE_KIND=serial AQA_SERIAL_PORT=COM3 \
AQA_FLASH_CMD="esptool.py --port COM3 write_flash 0x0 firmware.bin"

# 模拟设备（默认，真实运行生成的产品）
AQA_DEVICE_KIND=sim
```

新增设备类型只需实现 `aqa/adapters/device.py` 里 `DeviceAdapter` 的五个方法。

## Webhook 服务

```bash
pip install fastapi uvicorn
python -m aqa serve --port 8080
# GitHub 仓库 Settings → Webhooks → http://<host>:8080/webhook/github
```

## 目录结构

```
aqa/
├── contract.py          ★ 设备状态契约：别名归一化 + 生成后自检
├── generator.py         ★ AI 生成产品代码（清单 → 逐文件生成 → 自检失败再修）
├── stages.py            ★ 十层阶段实现（含测试设计、三级判定、建单、确认）
├── orchestrator.py      编排器：顺序执行、重试、审计日志
├── llm.py               模型客户端（DeepSeek / Qwen-VL / mock）+ 用量计量
├── config.py            环境配置
├── context.py           运行上下文 + 产物落盘
├── cli.py               run / demo / serve 命令
├── api.py               FastAPI webhook
└── adapters/
    ├── device.py        设备：Mock（剧本）/ Sim（真实执行）/ ADB / 串口 + 能力声明
    ├── build.py         构建：打包 zip / docker / mock
    └── bugtracker.py    缺陷：本地 JSON / GitHub Issue（按证据键去重）
tests/                   42 个测试（契约 / 判定 / 生成 / 端到端）
docs/ARCHITECTURE.md     架构设计与技术决策
```

## 产物

每次运行产出 `runs/<run_id>/`：

```
product.json       AI 生成的产品文件清单 + 状态契约自检结果
product/           生成的产品源码（可直接 python main.py boot 运行）
build.json         构建产物（zip + sha256）
deploy.json        部署结果（工作目录、方法）
testplan.json      AI 设计的测试计划
traces/            每个用例的屏幕快照 + 串口日志
traces.json        执行轨迹
verdicts.json      逐用例判定（judged_by 标明 rule/state/vision）+ 根因归因
report.md/.json    测试报告
bugs.json          提交的缺陷及确认状态
timeline.jsonl     全流程事件流（审计）
```

## 设计要点

- **设备状态契约**（`contract.py`）：AI 写的固件可能用 `fw_version` / `cloud.connected`
  这类字段名。平台把它们归一化成 `screen / online / version / ok` 四个规范字段，
  并在生成后**实跑一次自检**，不合规就把违规点喂回模型修一轮。历史事故：不做这层，
  字段名不一致会被当成「设备故障」，一轮产出 4 条假 bug。
- **三级证据链判定**：日志硬信号（panic / watchdog / mismatch）→ 设备状态对照用例预期 →
  视觉模型。前两级确定性强且几乎零成本，视觉只在前两级不可用时兜底。
- **两种「非通过」不算缺陷**：
  - `INCONCLUSIVE`（无法判定）：状态读不出来、证据不足
  - `SKIPPED`（未执行）：用例需要错误凭据 / 掉电 / 坏镜像等注入能力，当前设备不具备

  误报会污染缺陷库，比漏报更贵；通过率只按真正执行过的用例计算。
- **按证据键去重**：指纹取自 `rule:system_reset:provision` 这类现象键，而不是模型写的
  根因文案（换个措辞就成"新 bug"）。同一现象只建一条 issue，受影响用例记入 `affected_cases`。
- **注入能力声明**：设备用 `capabilities()` 声明支持哪些场景、能否注入故障；测试设计据此
  标注 `needs_injection`，跑不了的用例由执行层标为未执行。
- **失败语义分级**：构建/部署失败立即中止；单用例执行失败不影响其余用例结论。
- **模型可替换**：OpenAI 兼容协议，公有云 API / vLLM / Ollama 三种形态随意切换。

## 测试

```bash
python -m unittest discover -s tests -v
```

## 路线图

- [x] 十层骨架 + AI 生成产品 + 真实执行闭环
- [x] 设备适配器（Mock / Sim / ADB / 串口）+ 能力声明
- [x] 设备状态契约（别名归一化 / 生成后自检 / AI 自修复）
- [x] 三级证据链判定 + 无法判定 / 未执行语义
- [x] 按证据键去重、人工确认回写、模型用量核算
- [ ] 视觉定位指令集（点击坐标由 VLM 输出，取代硬编码）
- [ ] 测试用例库沉淀（通过用例自动转回归集）
- [ ] 设备农场（多设备并行、抢占调度）
- [ ] OTA 专项（断电 / 断网 / 回滚）

## License

MIT
