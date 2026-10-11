# ECHO-Repro 实验总结：SWE-bench Lite 复现结果与整体架构

> 更新日期：2026-10-10 ｜ 基线数据集：SWE-bench Lite（300 实例）
> 当前成绩：**F2P 188 / 300（62.7%）**，从首跑 60（20.0%）经过四轮迭代提升。

---

## 1. 项目概述

ECHO-Repro 是一个面向软件缺陷复现的研究原型：从 issue / log / trace / repository / environment files 自动生成**最小可执行复现 harness**。它不直接修 bug，而是合成一个可信的复现脚本——在 buggy 版本上复现问题、在 fixed 版本上通过，为后续自动修复、patch validation 和实验评测提供可靠 oracle。

跨仓库复现通常死在三个地方，本项目逐一应对：

| 失败点 | 应对机制 |
| --- | --- |
| 上下文不完整（issue 缺代码路径、测试风格、fixture） | BugSpec 结构化抽取 + 仓库检索 + concise context 构建 |
| 环境不可靠（依赖缺失、版本不匹配、commit 不对） | 按仓库缓存的 venv + LLM 依赖修复 + era-Python 老解释器 |
| 验证不严格（ImportError/SyntaxError 不等于复现） | 执行结果分类 + 严格 F2P 判定 + 反馈循环修复 |

---

## 2. 整体架构

### 2.1 主流程

```text
Issue / Log / Trace / Repo / Env Files
        │
        v
[1] BugSpec 抽取（当前行为/期望行为/失败签名/可疑符号）
        │
        v
[2] 上下文构建（repo cache + commit 检出 + 检索 source/test/env files）
        │
        v
[3] Harness 生成（LLM，mock / OpenAI / Anthropic 兼容客户端）
        │
        v
[4] 反馈循环（核心）──────────────────────────────┐
    buggy 执行 → 分类 → 按类别修复 → 换解释器重试 │
    reproduced 后跑 fixed 版 → F2P 判定             │
        │                                          │
        v                                          │
[5] 结果落盘 result.json（prompts / attempts /      │
    environment_repairs / final_result.is_f2p）     │
        │                                          │
        v                                          │
[6] 批量驱动 + 汇总（summarizer → md / csv）  ◀────┘
```

### 2.2 反馈循环（`src/echo_repro/feedback_loop.py`）

每实例最多 `max_attempts`（批量跑用 5）轮，每轮按 buggy 执行结果的分类走不同修复门：

```
buggy 执行 → classify_execution
  ├─ reproduced ──────────> 跑 fixed 版 → is_f2p？ 是 → 结束
  ├─ dependency_error ────> repair_dependency（LLM 提包名版本 → 白名单校验 → pip install）
  │                         修复成功且换 python_path → continue 重跑
  ├─ environment_error ───> repair_dependency（同上门，2026-10-09 起）
  │                         source_checkout 分支：pip install -e . 现场建扩展模块
  ├─ harness_error ───────> repair_harness（LLM 修 harness）
  ├─ resolved ────────────> repair_harness（强化 oracle）
  └─ oracle/repo/patch_error → break（不可修复）
```

分类规则在 [validator.py](../src/echo_repro/validator.py)：`ModuleNotFoundError` → dependency_error；`ImportError`/`broken installation`/`cannot import name` → environment_error；语法错/文件缺失 → harness_error；按最终 stdout 的 "Issue reproduced"/"Issue resolved" 判定部分成功。

### 2.3 环境层（`src/echo_repro/environment.py` + `dependency_repair.py`）

- **缓存 venv**：按仓库 slug 复用 `envs/<slug>`；宿主 Python 3.12 对老仓库不够。
- **era-Python venv**：`detect_python_version` 按代码特征定版本上限（`collections.Mapping`→3.9，`imp`/`distutils`→3.11），从 `~/.local/bin/python3.{8,9,10}`（uv）与 `/opt/homebrew/bin/python3.11` 找解释器，建 `envs/<slug>-pyNN`；`_legacy_build_env()` 注入 `-Wno-error=incompatible-function-pointer-types` CFLAGS。
- **LLM 依赖修复**：读 stderr 让 LLM 提议包+版本，严格校验（仅白名单 pip 形式、拒绝 --user/--target/--root、https-only 索引），2 轮带失败反馈，机械安装兜底。修复记录进 `result.json` 的 `environment_repairs[*].strategy`（"llm"/"mechanical"）。
- **修复 prompt 的版本 pin**：`setuptools<81`（84 移除了 pkg_resources）、`mpmath>=0.19`、`numpy==1.26.4`、`docutils==0.20.1`（拒 docutils-stubs 陷阱）、`py==1.10.0`。

### 2.4 LLM 客户端（`src/echo_repro/llm/`）

- 抽象层支持 mock / OpenAI 兼容 / Anthropic 兼容端点。
- **wall-clock 传输界**（macOS 特有坑）：SSL 读经 CONNECT 代理（127.0.0.1:7897）时 urllib 的 socket 超时**不生效**，端点劣化会挂 30+ 分钟。`_urlopen_with_wall_clock` 把 urlopen 放进守护线程、`join(timeout+30)`，超时抛 `RequestHungError` 且**不重试**（重试只会再挂）。实测 django-11422 单实例 8 次调用 1543s 全部有界。

### 2.5 批量驱动（`scripts/run_swebench_lite_full.py`）

- 3 worker 线程池，每实例一个独立 CLI 子进程（崩溃隔离、语义与单跑一致）。
- **resume 模式**：已有 result.json 的实例自动跳过 → 重跑批次天然保留既有 F2P。
- **同仓库不并发**：`inflight_repos` 集合 + round-robin 交错，避免 git shallow.lock 冲突。
- **每实例日志文件**（`outputs/_instance_logs/<id>.log`，追加 + offset 读尾）：CLI 结束前不打印任何东西，0 字节日志中途是正常的；这也是三个楔子 bug 教训的产物（见 §4.5）。
- **guard**：连续 6 个基础设施错误（repo_error/NO_RECORD）暂停 300s 并探测 git 连通性，4 次失败后中止。
- **watchdog**：95 分钟无完成即 `os._exit(3)`。
- 启动必须 `subprocess.Popen(..., start_new_session=True)` 脱离会话——Claude Code 会话退出会杀掉后台任务（macOS 无 setsid）。

### 2.6 模块清单

| 模块 | 职责 |
| --- | --- |
| [src/echo_repro/cli.py](../src/echo_repro/cli.py) | 入口：`run-swebench-one`（先备仓库、再跑反馈循环） |
| [src/echo_repro/feedback_loop.py](../src/echo_repro/feedback_loop.py) | 修复循环（核心） |
| [src/echo_repro/environment.py](../src/echo_repro/environment.py) | venv / era 解释器 / 修复执行 |
| [src/echo_repro/dependency_repair.py](../src/echo_repro/dependency_repair.py) | LLM 依赖修复 |
| [src/echo_repro/validator.py](../src/echo_repro/validator.py) | 执行结果分类 + F2P 判定 |
| [src/echo_repro/repo_manager.py](../src/echo_repro/repo_manager.py) | repo cache / clone / commit 检出 |
| [src/echo_repro/llm/](../src/echo_repro/llm/) | LLM 客户端（wall-clock 传输） |
| [src/echo_repro/models.py](../src/echo_repro/models.py) + [result_writer.py](../src/echo_repro/result_writer.py) | 实验记录 schema + 落盘 |
| [src/echo_ci/](../src/echo_ci/) | CI repair router MVP（独立赛道：CI 日志修复） |
| [scripts/run_swebench_lite_full.py](../scripts/run_swebench_lite_full.py) | 批量驱动 |
| [scripts/summarize_swebench_experiment.py](../scripts/summarize_swebench_experiment.py) | 结果汇总 → md / csv |

---

## 3. 结果总览

### 3.1 F2P 提升轨迹

![F2P 提升轨迹](f2p_trajectory.svg)

| 阶段 | 日期 | F2P | Δ |
| --- | --- | --- | --- |
| 基线首跑 | 10/4 | 60 | — |
| LLM 依赖修复 | 10/5–10/8 | 139 | +79 |
| era-Python venv 试验 | 10/9 | 142 | +3 |
| era 合并重跑 | 10/9–10/10 | 177 | +35 |
| NO_RECORD 清理 | 10/10 | 188 | +11 |

### 3.2 最终分类明细（300 实例）

| 类别 | 数量 | 说明 |
| --- | --- | --- |
| **F2P** | **188（62.7%）** | `final_result.is_f2p = true` |
| dependency_error | 30 | 缺依赖；LLM 修复仍失败或依赖链未解决 |
| environment_error | 27 | ImportError / broken installation / 扩展模块未构建 |
| harness_error | 23 | 生成的 harness 有语法/文件错误 |
| oracle_error | 11 | 复现逻辑本身不对（模型能力边界） |
| reproduced | 13 | 复现成功但 fixed 版未通过（部分成功） |
| resolved | 6 | buggy 版直接通过（部分成功） |
| repo_error | 1 | 仓库准备失败 |
| not_run | 1 | matplotlib-25433 持续 rc=2 抖动，单条重试中 |

### 3.3 成本

- 300 实例累计 **7,797,435 tokens**、**817 次 harness 生成 attempt**；
- F2P 实例平均成本 20,575 tokens；四轮迭代总计 +128 F2P。

---

## 4. 历次迭代与关键技术决策

### 4.1 首跑 60 → 依赖修复 139（+79）

首跑 300 实例中 **180 条死于 dependency_error**：缺失模块分布 mpmath 64 / distutils 61 / numpy 21 / docutils 14 / sklearn 5 / 其他 15。机械 `pip install <模块名>` 对 distutils/imp/pkg_resources 这类 stdlib-shim 完全无效（PyPI 上没有同名包）。引入 LLM 驱动修复后，5 实例试验 9/9 安装成功，全量重跑把 79 条 dependency_error 转成 F2P。

### 4.2 era-Python venv 142（+3）

缓存 venv 一直用宿主 Python 3.12 创建，老仓库撞 stdlib 移除墙（`collections.Mapping`、`imp`、`distutils` 已删除，pip 装不回来）。按代码特征检测所需版本上限并落到 3.8–3.11 老解释器 venv（§2.3）。试验证明：sympy-14317 在 `sympy__sympy-py39` + mpmath==0.19 下 F2P。

### 4.3 era 合并重跑 177（+35）

131 条（113 era + 18 pass-6）合并重跑，期间部署了两个关键修复：

- **environment_error 修复门**：astropy 类实例死于 "broken installation" 分类，此前永远进不了依赖修复。把 [feedback_loop.py](../src/echo_repro/feedback_loop.py) 的门从 `== "dependency_error"` 扩到 `in {"dependency_error", "environment_error"}`；`source_checkout` 分支对 astropy 式未构建扩展模块执行 `pip install -e .` 就地构建。stderr 不可解析时安全 no-op，零回归。借助 driver 的 `PYTHONPATH=src` 机制**热部署**，无需重启（worker 每次 spawn 都从源码加载）。
- **Mac 睡眠冻结**：机器空闲休眠 22:47–23:56，`time.monotonic()` 冻结导致所有超时/看门狗静默失效。插电唤醒后自愈（pytest-7168 醒来直接 F2P）；此后用 `caffeinate -i -m` 保活。

### 4.4 NO_RECORD 清理 188（+11）

20 条 rc=2 端点抖动（"Remote end closed connection without response"，多为 django/sphinx 集群）进入清理 pass：11 条转 F2P，3 条转 oracle_error（真实结果），其余为各类真实失败。

### 4.5 基础设施可靠性（三个楔子 bug + 一桩悬案）

| 问题 | 签名 | 修复 |
| --- | --- | --- |
| 管道楔子 | `capture_output=True` 在 communicate() 里等 EOF，CLI 的孙进程握着写端不放 → future 永不完成 | 每实例日志文件 + offset 读尾 + PYTHONUNBUFFERED=1 |
| 快照楔子 | `as_completed(list(pending))` 一次快照，快照期间新提交的 future 完成后无人处理，长任务卡死其他槽位 | 检测 `len(pending) > len(snapshot)-1` 即 break 重快照 |
| 空转楔子 | 重跑清单 ≤2 个 repo 时初始波 100% CPU 空转 | submit_next 无进展即 break |
| 悬案 | django-11422 的 buggy 工作目录在运行中凭空消失（FileNotFoundError） | 排除所有已知 rmtree 来源，未复现，继续观察 |

---

## 5. 剩余墙与下一步

| 墙 | 规模 | 建议动作 |
| --- | --- | --- |
| dependency_error + environment_error | 30 + 27 | 再跑一轮（LLM 修复有随机性，成本 ~4h） |
| harness_error | 23 | 针对性调 harness 修复 prompt |
| oracle_error | 11 | 模型能力/问题表述边界，换模型或改 prompt |
| reproduced / resolved 部分成功 | 19 | fixed 版失败原因分类后再定 |
| matplotlib-25433 | 1 | 单条重试已启动 |

---

## 附录 A：常用命令

```bash
# 单实例
.venv/bin/python -m echo_repro.cli run-swebench-one \
  --instances-file data/swebench_lite.jsonl --instance-id <id> \
  --workdir repos --cache-dir repos/cache --env-root envs \
  --output-root outputs --llm anthropic --max-attempts 5

# 批量（resume 模式默认保留已有结果；重跑失败清单时加 --no-resume）
.venv/bin/python scripts/run_swebench_lite_full.py \
  --llm anthropic --max-attempts 5 --workers 3 \
  --timeout-per-instance 4800 --watchdog-minutes 95 \
  --instance-ids <逗号分隔>

# 汇总
.venv/bin/python scripts/summarize_swebench_experiment.py \
  --instances-file data/swebench_lite.jsonl --outputs-dir outputs \
  --output-md outputs/swebench_lite_full_summary.md \
  --output-csv outputs/swebench_lite_full_summary.csv
```

环境变量：`ECHO_REPRO_LLM_TIMEOUT=300`、`ECHO_REPRO_LLM_MAX_TOKENS=32768`、`ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL=deepseek-v4-pro`。

## 附录 B：运维纪律（教训固化）

- **结果文件永不删除**：重跑前 `mv` 进 `outputs/_pre_rerun_*_backup_<stamp>/`。
- **批量驱动必须脱离会话启动**（`start_new_session=True`），严禁作为会话后台任务。
- **长跑保活**：`caffeinate -i -m` + 插电源；睡眠会冻结 monotonic 时钟，所有超时机制静默失效。
- **代理抖动**：rc=2 "Remote end closed connection" 是瞬时故障，进清理 pass 重试即可。
- **重跑清单从 result.json 现算**：`failure_category in ("repo_error", "no_record")`，不要复用旧清单。
