# ECHO-Repro

ECHO-Repro 是一个面向软件缺陷复现的研究原型，用于从 issue / log / trace / repository / environment files 自动生成最小可执行 reproduction harness。

它的目标不是直接修 bug，而是先合成一个可信的复现脚本：这个脚本应该在 buggy 版本上复现问题，在 fixed 版本上通过，从而为后续自动修复、patch validation 和实验评测提供可靠 oracle。

## 项目定位

跨仓库 bug reproduction 通常失败在三个地方：

- 上下文不完整：issue 里没有完整代码路径、测试风格、fixture 和配置细节。
- 环境不可靠：依赖缺失、版本不匹配、仓库没有 checkout 到正确 commit。
- 验证不严格：脚本失败不一定代表复现成功，可能只是 `ImportError`、`SyntaxError` 或人为断言。

ECHO-Repro 的核心思想是把 reproduction 当成一个环境感知的合成与验证问题，而不是单纯让 LLM 写一段测试代码。

## 当前能力

给定 issue 文本和 buggy/fixed 仓库，当前原型可以完成：

- 抽取结构化 `BugSpec`，包括当前行为、期望行为、失败签名、关键词和可疑符号。
- 从仓库中检索 source files、test files、environment files。
- 构建 concise reproduction context，减少 LLM 看到的无关内容。
- 调用 mock、OpenAI-compatible 或 Anthropic-compatible LLM 生成 `reproduce.py`。
- 将 harness 写入 buggy/fixed 仓库并执行。
- 根据执行结果区分 repo、patch、dependency、environment、harness 和 oracle 错误。
- 通过 feedback loop 对 harness 进行修复或增强 oracle。
- 遇到缺失依赖时自动创建/复用按 repo 缓存的 venv，并安装最小依赖。
- 对 SWE-bench Lite 实例准备 buggy/fixed 仓库，支持 repo cache 和 shallow fetch。
- 将一次运行写成稳定实验记录，包括 prompts、attempts、最终 harness 和 `result.json`。

## 核心原理

ECHO-Repro 的主流程如下：

```text
Issue / Log / Trace / Repo / Env Files
        |
        v
[1] Bug Specification Extraction
        |
        v
[2] Environment-aware Context Retrieval
        |
        v
[3] Concise Reproduction Context Construction
        |
        v
[4] Harness Synthesis
        |
        v
[5] Execution Feedback Loop
        |
        v
[6] Fail-to-Pass Validation
        |
        v
Minimal Validated Reproduction Harness
```

### 1. BugSpec 抽取

`bug_spec.py` 把原始 issue 转成结构化描述：

```text
BugSpec = {
  title,
  summary,
  current_behavior,
  expected_behavior,
  failure_signature,
  reproduction_hint,
  keywords,
  suspect_symbols
}
```

这样后续检索和生成都不直接依赖原始长 issue，而是围绕明确的失败签名和预期行为展开。

### 2. 环境感知检索

`retriever.py` 不只检索源码，还把上下文分成三类：

- Source context：可能触发 bug 的源码文件。
- Test context：已有测试、fixture、测试风格和 helper。
- Environment context：`requirements.txt`、`pyproject.toml`、`setup.py`、`tox.ini`、CI 配置等。

这样 harness 生成时不仅知道“bug 可能在哪”，也知道“项目通常怎么测试”和“需要什么环境才能跑起来”。

### 3. 简洁复现上下文

`context_builder.py` 把 BugSpec、源码片段、测试片段和环境片段压缩成一个 concise context。这个 context 是 LLM 生成 harness 的主要输入，目标是减少噪声，让模型聚焦于复现所需信息。

### 4. Harness 生成

`harness_generator.py` 调用 LLM 生成最小 `reproduce.py`。生成结果会经过 `code_cleaner.py` 清理 Markdown code fence，避免 LLM 输出格式直接破坏 Python 执行。

一个合格 harness 应该：

- 能在目标仓库内直接执行。
- 触发 issue 描述的真实行为。
- 不使用人为 `raise AssertionError("failed")` 伪造失败。
- 在 buggy 版本输出 `Issue reproduced`。
- 在 fixed 版本输出 `Issue resolved`。

### 5. 执行反馈循环

`feedback_loop.py` 根据执行结果决定下一步：

- `harness_error`：说明脚本本身有问题，可以让 LLM 修 harness。
- `oracle_error`：说明脚本能跑，但判定标准不够强，可以增强 oracle。
- `dependency_error`：说明缺依赖，交给 Environment Repair Loop。
- `environment_error`：说明环境或构建问题，不继续让 LLM 盲目改脚本。
- `repo_error` / `patch_error`：说明 benchmark 准备阶段不可信，直接记录失败。

这个设计避免把所有失败都当成 “LLM 生成错了”，尤其避免在环境没好的情况下让 LLM 反复改脚本。

### 6. Fail-to-Pass 验证

`validator.py` 使用 Fail-to-Pass 作为成功标准：

```text
buggy repo: Issue reproduced
fixed repo: Issue resolved
```

只有同时满足这两个条件，才认为复现成功。否则会记录具体 failure category，方便后续统计和消融实验。

## 代码结构

```text
echo-repro/
  README.md
  pyproject.toml
  src/echo_repro/
    cli.py
    config.py
    models.py
    pipeline.py
    bug_spec.py
    retriever.py
    context_builder.py
    prompts.py
    harness_generator.py
    code_cleaner.py
    executor.py
    validator.py
    feedback_loop.py
    environment.py
    repo_manager.py
    result_writer.py
    swebench_adapter.py
    llm/
      base.py
      mock_client.py
      openai_client.py
      anthropic_client.py
    utils/
  scripts/
    download_swebench_lite.py
    create_swebench_sample.py
    summarize_swebench_experiment.py
  examples/
    issue_example.txt
    mock_buggy_repo/
    mock_fixed_repo/
  tests/
  data/
  outputs/
  repos/
  envs/
  论文基础/
```

## 核心模块说明

| 模块 | 作用 |
| --- | --- |
| `cli.py` | Typer 命令行入口，提供本地运行、SWE-bench 预览、仓库准备和单实例运行命令。 |
| `config.py` | 从环境变量读取 LLM provider、model、base URL、temperature、timeout 等配置。 |
| `models.py` | 定义流水线核心数据结构，例如 `BugSpec`、`PreparedRepos`、`PipelineResult`。 |
| `pipeline.py` | 编排完整流程：BugSpec 抽取、检索、生成、执行和验证。 |
| `bug_spec.py` | 调用 LLM 或 mock client，从 issue 中抽取结构化 bug specification。 |
| `retriever.py` | 关键词检索 source/test/env 文件和片段。 |
| `context_builder.py` | 构造传给 LLM 的 concise reproduction context。 |
| `prompts.py` | 集中管理 BugSpec、harness generation、repair 和 oracle strengthening prompts。 |
| `harness_generator.py` | 调用 LLM 生成 `HarnessCandidate`。 |
| `code_cleaner.py` | 清理 LLM 生成代码中的 Markdown fence 和多余格式。 |
| `executor.py` | 写入并执行 harness，捕获 stdout、stderr、return code 和 timeout。 |
| `validator.py` | 分类执行失败类型，并执行 Fail-to-Pass 验证。 |
| `feedback_loop.py` | 根据执行反馈修复 harness、增强 oracle 或触发环境修复。 |
| `environment.py` | 解析 repo 环境 profile、复用缓存 venv，并在缺依赖时做最小修复。 |
| `repo_manager.py` | 准备 SWE-bench 仓库，校验 `.git`、base commit、patch 和 diff；支持 repo cache。 |
| `result_writer.py` | 输出 `result.json`、prompts、attempts 和最终 harness，便于实验审计。 |
| `swebench_adapter.py` | 读取 SWE-bench Lite JSONL，选择实例并提取 issue/repo metadata。 |

## LLM 后端

项目支持三种 LLM provider：

| Provider | 参数 | 说明 |
| --- | --- | --- |
| `mock` | `--llm mock` | 默认模式，不需要网络和 API key，适合单元测试和本地 smoke test。 |
| `openai` | `--llm openai` | OpenAI-compatible Chat Completions API。 |
| `anthropic` | `--llm anthropic` | Anthropic-compatible Messages API，可接 DeepSeek Anthropic 兼容端点。 |

### Mock 模式

```bash
echo-repro run-one \
  --issue-file examples/issue_example.txt \
  --buggy-repo examples/mock_buggy_repo \
  --fixed-repo examples/mock_fixed_repo \
  --llm mock
```

### OpenAI-compatible 模式

```bash
export OPENAI_API_KEY="..."
export OPENAI_BASE_URL="https://api.openai.com/v1"
export OPENAI_MODEL="gpt-4o-mini"
export OPENAI_TEMPERATURE="0.2"
```

```bash
echo-repro run-one \
  --issue-file examples/issue_example.txt \
  --buggy-repo examples/mock_buggy_repo \
  --fixed-repo examples/mock_fixed_repo \
  --llm openai
```

### Anthropic-compatible / DeepSeek 模式

```bash
export ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"
export ANTHROPIC_AUTH_TOKEN="..."
export ANTHROPIC_MODEL="deepseek-v4-pro[1m]"
```

```bash
echo-repro run-one \
  --issue-file examples/issue_example.txt \
  --buggy-repo examples/mock_buggy_repo \
  --fixed-repo examples/mock_fixed_repo \
  --llm anthropic
```

不要把真实 API key 写进仓库。建议只通过 shell 环境变量或本地 `.env` 注入。

## 安装

```bash
cd echo-repro
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

如果本地 `echo-repro` entrypoint 没有刷新，也可以用模块方式运行：

```bash
PYTHONPATH=src .venv/bin/python -m echo_repro.cli version
```

## 快速开始

运行内置示例：

```bash
echo-repro run-one \
  --issue-file examples/issue_example.txt \
  --buggy-repo examples/mock_buggy_repo \
  --fixed-repo examples/mock_fixed_repo \
  --llm mock
```

运行带反馈循环的示例：

```bash
echo-repro run-loop \
  --issue-file examples/issue_example.txt \
  --buggy-repo examples/mock_buggy_repo \
  --fixed-repo examples/mock_fixed_repo \
  --llm mock \
  --max-attempts 3
```

查看检索上下文：

```bash
echo-repro inspect-context \
  --issue-file examples/issue_example.txt \
  --repo examples/mock_buggy_repo \
  --llm mock
```

运行测试：

```bash
.venv/bin/python -m pytest
```

## SWE-bench Lite 工作流

下载 SWE-bench Lite：

```bash
python scripts/download_swebench_lite.py
```

生成 20 条小规模实验集：

```bash
python scripts/create_swebench_sample.py \
  --instances-file data/swebench_lite.jsonl \
  --output-file data/swebench_lite_small.jsonl \
  --sample-size 20
```

预览单个实例：

```bash
echo-repro swebench-preview \
  --instances-file data/swebench_lite.jsonl \
  --instance-id astropy__astropy-12907
```

准备 buggy/fixed 仓库：

```bash
echo-repro prepare-swebench \
  --instances-file data/swebench_lite.jsonl \
  --instance-id astropy__astropy-12907 \
  --workdir repos \
  --cache-dir repos/cache
```

运行单个 SWE-bench 实例：

```bash
echo-repro run-swebench-one \
  --instances-file data/swebench_lite.jsonl \
  --instance-id astropy__astropy-12907 \
  --workdir repos \
  --cache-dir repos/cache \
  --env-root envs \
  --env-python /path/to/python3.10 \
  --env-profile \
  --output-root outputs \
  --llm mock \
  --max-attempts 3
```

默认会启用轻量 `--env-profile` 检测，但不会自动安装完整 repo 环境。只有显式传入 `--allow-env-install` 时，才会创建缓存 venv 并尝试 `pip install -e .`；如果仓库需要旧 Python，可以用 `--env-python` 指向对应解释器，避免批量实验时每个实例都占用大量硬盘。

汇总实验结果：

```bash
python scripts/summarize_swebench_experiment.py \
  --instances-file data/swebench_lite_small.jsonl \
  --outputs-dir outputs \
  --output-md outputs/swebench_lite_small_summary.md \
  --output-csv outputs/swebench_lite_small_summary.csv
```

## Repo Cache 机制

真实 SWE-bench 实验最容易卡在 clone 阶段。ECHO-Repro 使用 `repos/cache/<repo_slug>` 作为仓库 cache：

```text
repos/cache/astropy__astropy/
repos/cache/django__django/
repos/cache/sympy__sympy/
```

首次遇到某个 commit 时，系统执行：

```bash
git fetch --depth=1 origin <base_commit>
```

后续同仓库实例会从 cache copy 出工作树，再 checkout 到目标 `base_commit`。仓库准备阶段会校验：

- prepared repo 必须存在 `.git`。
- buggy `HEAD` 必须等于 `base_commit`。
- fixed repo 必须从 buggy copy 而来。
- patch 必须能够 apply。
- patch apply 后必须产生真实 tracked diff。

这些校验可以避免把错误仓库、错误 commit 或空 patch 当作可用 benchmark 地基。

## Environment Repair Loop

运行 feedback loop 前，`environment.py` 会先生成 repo-level environment profile：

- 根据 `tox.ini`、`pyproject.toml`、`setup.cfg` 等文件检测推荐 Python 版本。
- 根据环境文件内容生成 dependency hash，形成可复用 profile key，例如 `astropy__astropy-py310-...`。
- 默认只复用已有 `envs/<profile_key>/`，不主动安装完整项目环境。
- profile 检测结果会写入 `result.json` 的 `environment_profile` 字段。

当 harness 执行出现缺失依赖时，`validator.py` 会分类为 `dependency_error`。随后 `environment.py` 会：

- 从 stderr 解析缺失模块，例如 `ModuleNotFoundError: No module named 'erfa'`。
- 将模块名映射到 pip 包，例如 `erfa -> pyerfa`。
- 在 `envs/<repo_slug>/` 或已存在的 profile venv 下创建/复用虚拟环境。
- 执行 `pip install <package>`。
- 用该 venv 的 Python 重跑 harness。
- 将安装命令、stdout、stderr、return code 和结果写入 `result.json`。

这个循环的目的不是提前安装完整项目环境，而是按需修复最小依赖，让 20 条小实验先跑到 harness generation 和 validation 阶段。

## 输出产物

每次 `run-swebench-one` 会生成：

```text
outputs/<instance_id>/
  result.json
  concise_context.md
  final_reproduce.py
  attempts.jsonl
  prompts/
    bug_spec.md
    generate_attempt_1.md
    repair_attempt_*.md
    strengthen_oracle_attempt_*.md
  attempts/
    reproduce_attempt_*.py
```

`result.json` 是主实验记录，包含：

- instance metadata：实例 ID、repo、base commit、patch 状态和 repo cache path。
- run config：LLM provider、model、temperature、max attempts 和 executor。
- bug spec：结构化 issue 描述。
- retrieval：检索到的 source/test/env 文件。
- environment repairs：依赖修复记录。
- environment profile：repo 环境 profile、Python 版本、依赖 hash 和缓存状态。
- attempts summary：每轮生成/修复的 prompt、harness、执行结果和 token usage。
- final result：buggy/fixed 状态、是否 F2P、失败类别和原因。

## 失败类型

ECHO-Repro 明确区分以下 failure category：

| 类型 | 含义 |
| --- | --- |
| `repo_error` | 仓库 clone/fetch/checkout 或 git metadata 校验失败。 |
| `patch_error` | fixed patch 无法 apply 或 apply 后没有真实 diff。 |
| `dependency_error` | Python 模块缺失，可尝试 Environment Repair。 |
| `environment_error` | 环境或二进制扩展问题，不应让 LLM 盲目改 harness。 |
| `harness_error` | harness 语法、路径或运行逻辑错误，可进入 LLM repair。 |
| `oracle_error` | harness 能跑但 oracle 不足或无法区分 buggy/fixed。 |
| `timeout` | 执行超时。 |
| `other` | 未归类失败。 |

这个分类是项目当前最重要的工程地基之一，因为它决定了失败后应该修 repo、修环境、修 harness，还是增强 oracle。

## 实验记录与当前状态

当前小规模实验目标是先跑 20 条 SWE-bench Lite，而不是直接跑 300 条。汇总表字段为：

```text
prepared? / env ready? / reproduced? / fixed passed? / failure category / cost / attempts
```

已经做过的 smoke test 包括：

- `astropy__astropy-12907`
- `django__django-10914`
- `sympy__sympy-11400`

这三条首先暴露的是 repo preparation / GitHub 网络访问问题，系统会记录为 `repo_error`。这说明当前错误已经被正确归因到 benchmark 地基，而不是误导 LLM 继续修改 harness。

## 测试结构

主要测试文件包括：

| 测试文件 | 覆盖内容 |
| --- | --- |
| `test_pipeline_mock.py` | mock LLM 完整流水线。 |
| `test_executor_validator.py` | harness 执行和 Fail-to-Pass 分类。 |
| `test_feedback_loop.py` | harness repair、oracle strengthening 和环境修复分支。 |
| `test_repo_manager.py` | repo cache、checkout、patch、diff 和 timeout 处理。 |
| `test_environment.py` | 缺失依赖解析、包名映射和 venv repair。 |
| `test_result_writer.py` | `result.json` 和实验产物输出。 |
| `test_swebench_adapter.py` | SWE-bench JSONL 读取和实例选择。 |
| `test_anthropic_client.py` | Anthropic-compatible DeepSeek 客户端。 |
| `test_openai_client.py` | OpenAI-compatible 客户端。 |

## 安全说明

ECHO-Repro 会把 LLM 生成的代码写入目标仓库并执行。当前执行器是本地 subprocess，不是强隔离沙箱。

使用真实开源仓库或第三方生成代码时，建议：

- 在 Docker、Vercel Sandbox 或其他隔离环境中执行。
- 不在宿主机暴露敏感环境变量。
- 不把真实 API key 写入 README、测试或 committed config。
- 对 `outputs/`、`repos/`、`envs/` 做清理和审计。

## 当前限制

- 检索仍是关键词级别，还没有 BM25、embedding 或函数级 chunking。
- harness 生成仍是单候选，没有多候选排序。
- feedback loop 还是启发式状态机，没有完整 trace matching。
- 执行不是 Docker 化，隔离性不足。
- 真实 SWE-bench 运行仍受 GitHub 网络、依赖编译和系统库影响。
- Environment Repair 只覆盖 Python 缺包类问题，不覆盖复杂系统依赖。

## 后续路线图

- 引入 BM25 / embedding / function-level retrieval。
- 加入 Docker 沙箱执行。
- 支持多候选 harness generation 和 ranking。
- 加强 failure signature / stack trace matching。
- 为 repo cache 增加 mirror URL、代理和预热脚本。
- 把 20 条 SWE-bench Lite 小实验完整跑通并输出可汇报表格。
- 进一步支持 patch discrimination validation，让 reproduction harness 服务于自动修复 patch 选择。
