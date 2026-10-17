# 验收与证据分层

本目录中的注入替身测试验证应用控制逻辑，不构成真实模型端到端测试。

| 层次 | 覆盖 | 证据 |
|---|---|---|
| 权限与输入 | 未登录、成人升权、Origin、输入限制、撤销 | `test_security.py` |
| 会话一致性 | 只记录已确认播放内容、错序确认、打断迟到结果、停止、隔离 | `test_sessions.py` |
| 安全失败关闭 | 输入危险规则、输出规则、审核拒绝/故障/超时 | `test_safety.py` |
| 降级诊断与挂断 | 原因码、阶段异常与超时、固定语音失败后恢复、挂断精确词 | `test_fallback_reasons.py` |
| 电话界面与采音 | 自动续听、静音、主动PTT、旧帧丢弃、挂断释放 | `frontend/session.test.cjs`、`frontend/audio.test.cjs` |
| 浏览器连续通话 | 一次拨打、多轮真实模型、语音打断、安全回复后继续、静音恢复、挂断 | `scripts/test_call_browser.cjs` 的 JSON |
| 真实语音端到端 | 本地 TTS 合成成人测试话术 → ASR → LLM → 独立审核调用 → TTS → ASR 回读，另测固定谜题与真实语音停止 | `scripts/test_e2e.py` 的 JSON |
| 真实模型压力 | 并发 1、2、4、8，请求延迟分位、明确过载、RSS、结束后恢复 | `scripts/stress_test.py` 的 JSON |

真实模型脚本使用正在运行的 HTTP/WebSocket 服务，禁止注入 mock。测试语音来自本地合成器，不使用儿童录音。报告明确记录模型健康信息、合成输入转写、输出、失败、原始样本与分位数；失败不改写成成功，过载拒绝与模型错误分别统计。

自动化通过不等同于 Android/iOS 真机麦克风、外放回声、低音量童声或儿童使用安全认证。真机验收仍需照护者在前台确认：权限、连续轮次、普通讲话打断、停止、锁屏、切后台、断网与耳机/外放。涉及机器环境或浏览器安全上下文的限制应写入交付报告。

报告中的 `generated` 表示经过本地 LLM 生成、独立审核和 TTS；`curated` 表示已核对谜题库，经独立审核和 TTS，`llm_ms=0`；`fallback` 为风险规则或模型故障触发的固定安全话术。三者必须区分，不能把固定话术视为生成模型通过。

最终压力命令使用 `--rounds 10 --concurrency 1 2 4 8 --require-overload`，共 150 个计分请求，预热及恢复各 1 个请求另记。8 并发同时覆盖连接上限与推理上限；正常容量拒绝独立计数，同时如实保留包含拒绝的总失败率。

独立审核的 20 条固定合成语料位于 `tests/fixtures/moderation-cases.json`，可用 `.venv/bin/python scripts/test_e2e.py --moderation-only` 复跑。报告默认另存 `reports/moderation-retest.json`；保留初测 `reports/moderation.json` 中的漏判/误拒事实。输出规则弥补已发现样本不代表独立小模型审核本身达到全对。

浏览器真实语音发现“故事角色采食未知蘑菇”漏判后，新增实际原文与普通熟食的应用回归。修订后的独立审核语料为 `tests/fixtures/moderation-final-policy-cases.json`（原 20 条保持不变，再加 1 条），使用 `--moderation-only --moderation-cases tests/fixtures/moderation-final-policy-cases.json --output reports/moderation-final-policy.json` 重跑。策略修订后的小规模压力回归另存 `reports/stress-final-policy.json`，不覆盖 150 请求主压力证据。

0.2.0电话验收使用可控的WebAudio虚拟麦克风按顺序注入本地MeloTTS音频，保留真实AudioWorklet/VAD、WebSocket、全部本地模型和浏览器播放完成ACK。不用文本请求替代录音，也不人工发送播放确认。预授权虚拟麦克风；浏览器自动播放策略保持默认。`call_behavior_passed`与`three_completed_generated_turns_met`分别报告连续通话控制和至少三轮生成完整播放结果；安全固定回应不能计为成功生成。运行命令：`node scripts/test_call_browser.cjs`。要求与旧浏览器脚本相同的本机Node、Playwright和Chrome路径，以及已预热的服务。


0.3.0音色管理权限、参考文件校验、失败恢复与缓存测试在`test_voices.py`；引擎配置/快照测试在`test_voice_engine.py`，浏览器录音清理与上传转换在`frontend/voices.test.cjs`。真实模型的多音色、克隆和小并发脚本是`scripts/test_voices.py`，真实家长UI脚本是`scripts/test_voice_browser.cjs`。必须使用隔离的服务器数据目录；它们会改变测试家庭所选音色，完成后还原并删除自己创建的合成测试音色。

试听波形有随机变化，不能要求相同音色每次SHA256完全一致。哈希只记录素材差异，参数快照和请求/返回ID核对路由；ASR回读只诊断内容可懂度，均不证明真人音色相似度。所有克隆参考都是本机Melo合成，不使用真实用户声音。
