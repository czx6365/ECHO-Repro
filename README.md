# ECHO-Repro

ECHO-Repro 是一个轻量级研究原型，用于环境感知的缺陷复现 harness 合成。

给定 issue 描述、可选日志/追踪信息，以及本地仓库路径，这个 MVP 可以：

1. 抽取结构化的 `BugSpec`
2. 检索相关源码、测试文件和环境/配置文件
3. 构建简洁的复现上下文
4. 生成一个最小可执行 harness，通常是 `reproduce.py`
5. 在 buggy 仓库上执行该 harness，并可选地在 fixed 仓库上执行
6. 在需要时通过反馈循环修复 harness 或增强 oracle
7. 验证是否满足 Fail-to-Pass

第一版故意保持简单，默认不依赖真实 LLM，能够本地直接运行。

## 项目目的

跨仓库复现缺陷经常失败，因为 issue 文本、源码上下文、测试以及环境假设往往分散在不同位置。ECHO-Repro 的目标是提供一条紧凑的流水线，把这些输入转成一个可运行的复现 harness。

## MVP 架构

```text
Issue 文本 + 可选日志
        |
        v
  bug_spec.extract_bug_spec
        |
        v
 retriever.retrieve_context
        |
        v
 context_builder.build_concise_context
        |
        v
 harness_generator.generate_harness
        |
        v
 feedback_loop.run_feedback_loop
        |
        v
 validator.validate_fail_to_pass
```

## 安装

```bash
cd echo-repro
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## 快速开始

运行完整的 mock 流水线：

```bash
echo-repro run-one \
  --issue-file examples/issue_example.txt \
  --buggy-repo examples/mock_buggy_repo \
  --fixed-repo examples/mock_fixed_repo \
  --mock
```

运行带反馈修复循环的流程：

```bash
echo-repro run-loop \
  --issue-file examples/issue_example.txt \
  --buggy-repo examples/mock_buggy_repo \
  --fixed-repo examples/mock_fixed_repo \
  --max-attempts 3 \
  --mock
```

只查看检索结果和简洁上下文：

```bash
echo-repro inspect-context \
  --issue-file examples/issue_example.txt \
  --repo examples/mock_buggy_repo
```

查看当前版本：

```bash
echo-repro version
```

把 SWE-bench Lite 的 `test` split 下载成本地 JSONL：

```bash
python scripts/download_swebench_lite.py
```

该脚本会把所有实例写入 `data/swebench_lite.jsonl`，并打印：

- 实例总数
- 前 5 个 `instance_id`

建立一个固定的小规模真实实验集：

```bash
python scripts/create_swebench_sample.py \
  --instances-file data/swebench_lite.jsonl \
  --output-file data/swebench_lite_small.jsonl \
  --sample-size 20
```

该脚本会优先保留 `astropy__astropy-12907` 作为 motivating example，并按仓库轮转补齐样本，避免小样本被单个仓库占满。

汇总小实验当前运行状态：

```bash
python scripts/summarize_swebench_experiment.py \
  --instances-file data/swebench_lite_small.jsonl \
  --outputs-dir outputs \
  --output-md outputs/swebench_lite_small_summary.md \
  --output-csv outputs/swebench_lite_small_summary.csv
```

汇总表包含：`prepared?`、`env ready?`、`reproduced?`、`fixed passed?`、`failure category`、`cost` 和 `attempts`。

把一个 SWE-bench 实例准备成本地 buggy/fixed 仓库：

```bash
echo-repro prepare-swebench \
  --instances-file data/swebench_lite.jsonl \
  --instance-id django__django-12345 \
  --workdir repos/ \
  --cache-dir repos/cache
```

`--cache-dir` 默认是 `repos/cache`。ECHO-Repro 会为每个仓库维护一个本地 cache，例如 `repos/cache/astropy__astropy`，首次遇到某个 commit 时执行 `git fetch --depth=1 origin <commit>`，后续实例直接从 cache copy/checkout，避免 20 条小实验反复卡在完整 clone 阶段。

对一个准备好的 SWE-bench 实例运行 ECHO-Repro：

```bash
echo-repro run-swebench-one \
  --instances-file data/swebench_lite.jsonl \
  --instance-id django__django-12345 \
  --workdir repos/ \
  --cache-dir repos/cache \
  --mock \
  --max-attempts 3
```

这个命令会准备 buggy/fixed 仓库、运行复现流水线、打印简洁摘要，并把完整实验记录保存到：

`outputs/<instance_id>/result.json`

## 输出产物

每次执行 `run-swebench-one` 都会在下面目录生成一份适合研究分析的记录：

```text
outputs/{instance_id}/
  result.json
  concise_context.md
  final_reproduce.py
  attempts.jsonl
  prompts/
  attempts/
```

关键文件说明：

- `result.json`：稳定的实验记录，带 `schema_version`
- `concise_context.md`：用于生成 harness 的完整上下文
- `final_reproduce.py`：最终生成或修复后的 harness
- `attempts.jsonl`：每次生成/修复尝试对应一条 JSON 记录
- `prompts/`：BugSpec 抽取和每次 harness 尝试的 prompt 快照
- `attempts/`：每次尝试产生的 harness 代码

可以通过 `result.json` 检查：

- 实例元数据
- 运行配置
- 检索到的 source/test/env 文件
- 最终状态以及是否满足 Fail-to-Pass
- 其他产物文件路径

这套结构适合做：

- ablation experiments
- prompt 对比
- repair loop 分析
- 离线审计系统实际执行了什么

## 示例行为

项目内置的示例模拟了一个简单 bug：

`buggy_module.divide(a, b)` 在 `b == 0` 时返回 `0`，但正确行为应当是抛出 `ZeroDivisionError`。

生成出来的 harness 会检查：

- 在 buggy 仓库中打印 `Issue reproduced`
- 在 fixed 仓库中打印 `Issue resolved`

## 模块说明

- `models.py`：流水线中的类型化数据模型
- `config.py`：基于环境变量的配置
- `bug_spec.py`：把 issue 文本抽取成 `BugSpec`
- `retriever.py`：对源码、测试和环境文件进行简单关键词检索
- `context_builder.py`：构建紧凑复现上下文
- `harness_generator.py`：把上下文转成可执行 Python harness
- `feedback_loop.py`：根据执行反馈修复 harness 或增强 oracle
- `repo_manager.py`：为 SWE-bench 风格实例执行 repo cache、shallow fetch、checkout、copy 和 patch
- `executor.py`：将 harness 写入仓库并通过子进程执行
- `validator.py`：执行结果分类与 Fail-to-Pass 验证
- `pipeline.py`：整体流程编排
- `llm/`：可插拔 LLM 客户端，包含 `MockLLMClient`
- `utils/`：文件和日志辅助工具
- `result_writer.py`：把一次运行写成稳定实验记录和配套产物

## LLM 配置

MVP 默认使用 `MockLLMClient`，不需要网络，也不需要 API key。

显式使用 mock 模式：

```bash
echo-repro run-swebench-one \
  --instances-file data/swebench_lite.jsonl \
  --instance-id django__django-12345 \
  --workdir repos/ \
  --cache-dir repos/cache \
  --llm mock \
  --max-attempts 3
```

如果要使用 OpenAI-compatible 模式，可以通过环境变量配置：

```bash
export OPENAI_API_KEY="..."
export OPENAI_BASE_URL="https://api.openai.com/v1"  # 可选
export OPENAI_MODEL="gpt-4o-mini"
export OPENAI_TEMPERATURE="0.2"
```

然后通过 `--llm openai` 运行：

```bash
echo-repro run-swebench-one \
  --instances-file data/swebench_lite.jsonl \
  --instance-id django__django-12345 \
  --workdir repos/ \
  --cache-dir repos/cache \
  --llm openai \
  --max-attempts 3
```

`--mock` / `--no-mock` 仍然保留为向后兼容别名，但更推荐使用：

`--llm mock|openai`

## 安全说明

本项目只会把生成代码写入目标仓库目录内部，并在该目录中执行。

但这仍然不构成强隔离。真正用于生产级评测时，应该在 Docker 或类似沙箱边界内执行生成 harness，再用在不受信任的仓库上。

## 当前 MVP 限制

- 检索目前仍然只是关键词级别
- harness 生成仍然是单候选
- 反馈修复仍然是单候选且偏启发式
- 会自动清理 LLM 输出中的 Markdown code fence，但还没有完整的代码规范化/静态修复流程
- 执行仍然是本地 subprocess，不是容器化
- 对跨语言仓库支持还不完善
- mock LLM 仍然是规则驱动的简化实现

## 真实仓库环境策略

SWE-bench Lite 有 300 个实例，但不建议一开始为 300 个实例逐个完整安装环境。更现实的策略是：

- 先按仓库聚类：同一个 `repo` 共享一套基础环境，例如 `astropy/astropy` 只准备一次基础 venv
- 按需安装：先运行 harness，只有遇到 `dependency_error` 时再解析缺失包并补装
- 缓存环境：把环境缓存到 `envs/{repo_slug}/`，多个实例复用
- 优先最小依赖：先安装项目的核心运行依赖，不先跑完整测试依赖矩阵
- 记录失败：如果依赖需要系统库或编译失败，把它记录到 `result.json` 的 failure category，留给后续批处理策略

对于当前 `astropy__astropy-12907`，失败点是缺少运行依赖 `erfa`。这类问题应该通过“按需补装 + 环境复用”解决，而不是为所有实例提前全量安装。

当前实现中，`run-swebench-one --max-attempts ...` 会启用 Environment Repair Loop：如果 buggy 执行被分类为 `dependency_error`，系统会解析缺失模块，映射到 pip 包名，在 `envs/{repo_slug}/` 下创建或复用 venv，安装后用该 venv 的 Python 重跑 harness。安装结果会写入 `result.json` 的 `environment_repairs` 和对应 attempt 记录。

## 后续路线图

- BM25 检索
- embedding 检索
- 函数级 chunking
- Docker 沙箱执行
- 更完整的 SWE-bench Lite 集成
- 多候选排序

## 之后如何进一步接入 SWE-bench Lite

项目目前已经在 `swebench_adapter.py` 中支持了 SWE-bench 风格 JSONL 的轻量适配。

如果要先从公开数据集生成本地 JSONL，可以使用：

```bash
python scripts/download_swebench_lite.py
```

如果只想预览某个实例，而不下载或执行完整 benchmark，可以运行：

```bash
echo-repro swebench-preview \
  --instances-file data/instances.jsonl \
  --instance-id django__django-12345
```

当前已经支持：

- 读取 JSONL 实例文件
- 通过 `instance_id` 选择单个实例
- 提取 issue 文本作为 ECHO-Repro 输入
- 准备带 repo metadata 的轻量 stub
- 基于 `repo`、`base_commit` 和 `patch` 准备单实例的本地 buggy/fixed 仓库

之后可以继续补的方向：

- 从 benchmark metadata 更完整地物化 buggy/fixed 仓库
- 把 `problem_statement` 直接接到 `run-one` 或 `run-loop`
- 利用 `patch` 和 `test_patch` 做更丰富的验证和分析
- 在批量运行前加入 Docker 隔离执行
