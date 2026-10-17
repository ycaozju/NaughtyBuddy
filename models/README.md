# 本地模型与运行时

本项目默认把语音识别、回答生成、独立回复审核和语音合成都放在当前机器上运行。运行期间不下载模型，不调用云端推理。首次安装需要访问以下公开模型源；音频和聊天内容不参与下载请求。

## 已部署的版本

| 用途 | 权重及体积 | 推理方式 | 来源与许可 |
| --- | --- | --- | --- |
| 生成与独立审核 | Qwen2.5-1.5B-Instruct，Q4_K_M，1,117,320,736字节 | llama.cpp，CPU，4线程，2个并发槽，每槽4096上下文 | [Qwen官方模型卡](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF)，Apache-2.0；从Qwen官方ModelScope镜像下载 |
| 语音识别 | SenseVoiceSmall int8，ONNX约229MiB | sherpa-onnx，CPU，2线程，中文，标点和数字规范化 | [FunAudioLLM模型卡](https://huggingface.co/FunAudioLLM/SenseVoiceSmall)，[sherpa转换模型说明](https://k2-fsa.github.io/sherpa/onnx/sense-voice/pretrained.html)；模型使用[FunASR MODEL_LICENSE](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE)，不是Apache-2.0 |
| 默认语音合成 | MeloTTS-Chinese，ONNX约163MiB | sherpa-onnx，CPU，2线程，speaker 0，speed 0.95 | [MyShell.ai模型卡](https://huggingface.co/myshell-ai/MeloTTS-Chinese)，MIT；[sherpa转换模型说明](https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/vits.html) |
| 多音色语音合成 | Kokoro v1.1-zh int8，ONNX约110MiB、音色数据约52MiB | sherpa-onnx，CPU，2线程，24kHz；模型103音色，其中100个中文，界面精选4个 | [Hexgrad官方模型卡](https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh)，Apache-2.0；[sherpa官方使用说明](https://k2-fsa.github.io/sherpa/onnx/tts/all/Chinese-English/kokoro-multi-lang-v1_1.html) |
| 本地参考声音克隆 | ZipVoice-distill int8编码器约5.4MiB、解码器约119MiB，Vocos约51.6MiB | sherpa-onnx，CPU，2线程，4步采样，24kHz；参考WAV＋逐字文本，不训练新权重 | [ZipVoice官方模型卡](https://huggingface.co/k2-fsa/ZipVoice)：Emilia预训练权重为CC-BY-NC，代码为Apache-2.0；[Vocos官方模型卡](https://huggingface.co/charactr/vocos-mel-24khz)标注MIT；[sherpa使用说明](https://k2-fsa.github.io/sherpa/onnx/tts/zipvoice.html) |

ASR权重原作者为FunAudioLLM/阿里巴巴，转换包由sherpa-onnx项目发布；默认MeloTTS权重原作者为MyShell.ai；新增Kokoro和ZipVoice的作者与许可分别见上表。发布或再分发时须保留作者、模型名称及各包许可。SenseVoice的自定义模型协议单独适用，不能将运行时的Apache-2.0许可套用到权重。

运行时为sherpa-onnx 1.13.8、Python 3.12.3、NumPy 2.5.4。llama.cpp使用提交`d7bd3bfcad3e29c7e49fd26f38c79ee3e9a3fd6b`（build 10680），从本机已有源码构建到项目`.runtime/llama-cpu`，源码仓库未修改；关闭CUDA、HIP和Vulkan。运行时不依赖ROCm，仍依赖本机glibc、libstdc++、OpenMP和OpenSSL。硬件为当前AMD Ryzen AI MAX+ 395机器，本轮全部推理使用CPU。

安装后的完整Python依赖版本见`requirements-lock.txt`。基础模型权重、压缩包和校验结果见`manifest.json`，三个下载文件的SHA-256固定在`../scripts/setup_models.py`。新增语音模型见`voice-manifest.json`，三个官方发布资产合计310,351,414字节（约296MiB下载量），SHA-256固定在`../scripts/setup_voices.py`。每次安装均核验，解压采用Python安全data filter；新增压缩包及解压文件约占600MiB。

## 重新安装

需要Python 3.12、venv、Git、curl、CMake、C/C++构建工具和OpenSSL开发包。当前机器已具备。全部安装留在项目内，不修改系统Python或现有模型服务。

```bash
cd /home/timocy/babybot
python3 -m venv .venv
.venv/bin/pip install -r requirements-lock.txt
.venv/bin/python scripts/setup_models.py --llama-source /home/timocy/llama.cpp
.venv/bin/python scripts/setup_voices.py
.venv/bin/python scripts/prepare_safe_audio.py
```

其他机器可以不传`--llama-source`，脚本会下载官方llama.cpp源码并检出固定提交。已有运行时只需下载权重时使用`--skip-runtime`。环境代理不通时可以加`--direct`；ModelScope默认直连。为压缩包、解压目录、构建产物和虚拟环境预留至少4GiB磁盘空间。

固定安全话术位于`.runtime/safe_audio`，由本地TTS预生成9类安全回复及1条AI身份开场，共10个WAV。准备脚本会校验话术和音频哈希，话术改变后自动重做；也可传`--force`。准备完成后重启应用，使启动时的音频缓存生效。模型中断或审核拒绝时可以播放这些固定内容，不必临时调用TTS。开场通过独立`greeting`事件发送，不计作生成回答。

独立启动LLM的命令如下；通常使用项目顶层启动脚本即可。

```bash
.runtime/llama-cpu/bin/llama-server \
  --model models/qwen2.5-1.5b-instruct-q4_k_m.gguf \
  --host 127.0.0.1 --port 8081 --ctx-size 8192 --parallel 2 \
  --threads 4 --threads-batch 4 --poll 0 --poll-batch 0 \
  --alias babybot-local --log-disable
```

只监听本机回环地址，且关闭llama.cpp日志。Python客户端忽略环境HTTP代理并拒绝非回环LLM地址。手机只访问应用服务，不直接连接模型服务。

## Provider接口和资源控制

`babybot.providers.LocalProviders(settings=None)`接受配置对象或字典，支持`models_dir`、`llm_url`、`model_timeout_s`、`speech_threads`。默认LLM地址为`http://127.0.0.1:8081/v1`，单模型超时8秒，语音推理2线程；应用另外限制整轮12秒、回复审核4秒。`health()`首次加载ASR及默认Melo权重，返回`ok`及`llm/asr/tts`布尔值。Kokoro和ZipVoice按需加载；`engine_available(models_dir, engine)`只检查必需文件是否存在，实际就绪以试听和固定话术准备成功为准。

- `transcribe(pcm, sample_rate=16000)`接收单声道、有符号16位小端PCM，不带WAV文件头；最长30秒。支持8/16/22.05/24/44.1/48kHz，普通网页链路使用16kHz。静音返回空文本。
- `generate(messages, mode)`使用应用给出的系统规则，本地最多生成96 tokens。应用的猜谜模式使用固定题库，其余三种玩法调用此方法。
- `moderate(user_text, answer)`发起独立、无历史的模型分类调用，语法约束只允许`ALLOW`或`DENY`；只有精确`ALLOW`放行。接口失败或超时抛错，由应用阻止播放。审核与生成复用同一套小模型权重，不代表独立安全模型或安全认证。
- `synthesize(text, voice=None)`返回单声道、16位PCM的WAV字节：默认Melo为44.1kHz，Kokoro和ZipVoice为24kHz。底层接收1至300字符；应用对Melo回答保留80字完整句上限，对新增音色限制40字完整句，并在合成前完成审核。`voice`是服务器生成的快照，包含`engine`及预设`speaker_id`，或ZipVoice的`reference_audio_path`和`reference_text`；浏览器不能提交路径。调用在等待TTS执行器前深复制快照，避免并发切换音色串音。
- `close()`关闭HTTP连接和线程执行器。

ASR和TTS各有一个单线程执行器，分别串行访问原生模型。模型实例按引擎缓存，但speaker、参考声音和参考文本逐请求传入，不使用全局可变的当前音色。任务取消后，原生推理可能仍短暂继续；并发许可要到原生调用实际完成才释放，避免打断后重入模型。超时不会无限新建CPU任务；应用另有会话、请求和音频长度限制。

## 本机单组件实测

以下为2026-10-13最初的组件探测，使用旧配置：ASR/TTS各4线程，LLM为8线程。最终交付已调整为语音2线程、LLM4线程，下面的数据不代表最终配置的性能。该次模型全冷加载约2.53秒。用三个普通中文短句合成后回读ASR，正文一致，仅标点不同；这是合成语音验证，不能代表真实5岁童声或手机外放环境的识别率。

| 输入 | 音频长度 | TTS耗时 | TTS实时率 | ASR耗时 |
| --- | ---: | ---: | ---: | ---: |
| 我们一起讲一个小兔子的故事吧。 | 2.473s | 0.222s | 0.090 | 0.046s |
| 小兔子捡到一颗种子。你想把它种在花园，还是森林里？ | 4.245s | 0.390s | 0.092 | 0.055s |
| 谢谢你告诉我。我们先停下来，找一位让你安心的大人帮忙。 | 4.714s | 0.409s | 0.087 | 0.060s |

实时率为计算耗时除以音频长度，越小越快。另测官方5.592秒普通话样本，冷加载加识别1.020秒，热识别0.059秒。完整应用端到端与并发结果以项目测试报告为准，不将这里的组件耗时当作完整等待时间。

1.5B小模型会出现语义错误和审核漏判。原始20项独立审核测试18项符合预期，1项假装真人的回复被漏放、1项安全劝停被误拒；规则层已拦截已知真人表述，但没有改变这份原始模型成绩。谜语因生成质量不稳定改为固定题库，仍进行审核。它适合验证本地工程链路，不能替代家长陪同的内容观察，也不能从这些测试推出儿童安全保证。完整应用验收以`docs/TEST_REPORT.md`为准。


## 多音色与克隆的本机验证

最终预设为Melo默认声，以及Kokoro的SID 3、20、58、59（两个中文女声、两个中文男声）。首次候选男声SID 74在40字固定安全回复中出现8.10秒耗时，因此在交付预设中移除；原报告仍保留，不把失败记录改成通过。模型自身仍有103音色，应用只开放这4个经过边界测试的中文预设。

短句“你好呀，我们一起讲一个小兔子的故事。”在2线程CPU实测：Melo约0.47–0.48秒；Kokoro最初候选约1.88–2.83秒；7.02秒Melo合成参考的ZipVoice约1.11–1.14秒。ASR回读正文一致，只说明句子内容可辨认，不能证明声音相似度。冷加载ZipVoice约0.72秒，Kokoro约0.84秒，实际延迟随机器负载和文本变化。

长句边界曾超过原8秒模型期限：69字实测长句时，Kokoro旧男声SID 74为10.78秒，20秒参考的ZipVoice为8.86秒。因此应用保留8秒单模型及12秒整轮期限，对新增音色的生成回答限制为完整的40字以内；固定安全话术不裁剪。新音色的处理中轮次限制为1，防止TTS排队放大延迟。

最终4个Kokoro预设对最长40字固定安全回复各测3次，均在8秒内；ZipVoice使用20秒纯合成参考，对同一40字回复及26字试听各测3次，也都在8秒内。具体数值和所有原始超限记录见：

- `../reports/voice-engine-benchmark.json`：初始短句、合成参考及可理解度。
- `../reports/voice-engine-boundary.json`：2/4线程、长句和20秒参考的原始边界。
- `../reports/voice-engine-preparation.json`：包含已淘汰SID 74超限的原始固定话术准备测试。
- `../reports/voice-engine-final-presets.json`：最终SID 3/20/58/59的最长固定话术结果。

复现命令（需要CPU可用，避免与压力测试同时运行）：

```bash
.venv/bin/python scripts/benchmark_voices.py --engine melo
.venv/bin/python scripts/benchmark_voices.py --engine kokoro
.venv/bin/python scripts/benchmark_voices.py --engine zipvoice
.venv/bin/python scripts/benchmark_voice_presets.py
```

本轮克隆测试只使用本机Melo生成的合成素材，没有用户本人的声音样本，没有读取机器里的私人录音；不能据此宣称已生成用户音色或验证相似度。实际创建个人音色需要使用者自己的录音和与其一致的转录文本。参考录音留在家庭数据目录，可由家长删除；运行时不向任何云端TTS发送录音。ZipVoice的Emilia权重带非商业限制，家庭自用以外的商业发布或再分发需要重新核对授权，不能把代码许可当作权重许可。
