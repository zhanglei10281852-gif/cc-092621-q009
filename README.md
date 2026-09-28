# 寺院香火与修缮协同服务

这是一个供寺院管理机构、文物保护人员和现场值守团队使用的 Python 后端。服务把寺院与殿堂档案、香火活动画像、环境观测、安全隐患、通风处置、修缮计划、殿堂封闭和操作审计保存在同一个 SQLite 数据库中，不依赖另行部署的数据库、缓存、消息队列或浏览器界面。

## 已有能力

- 寺院与殿堂：登记古建、城市、山地和社区寺院，维护殿堂参访顺序、预计停留时间、通风容量和开放状态。
- 香火活动画像：按日常上香、节庆、法会、纪念活动和参访团保存颗粒物、一氧化碳、送排风与风险优先级目标。
- 安全策略：校验环境风险评分权重、严重度阈值、通风倍率和处置时长，支持草稿、发布、生效和退役状态。
- 环境观测：使用业务观测键幂等写入人流密度、PM2.5、一氧化碳与送排风数据，异常观测会形成可跟踪的安全隐患。
- 通风处置：校验值守授权与生效策略，按殿堂容量预留送排风资源，支持完成、取消和超时释放。
- 修缮协同：维护分阶段修缮活动、目标殿堂、计划时间和执行状态，并可登记殿堂封闭窗口阻止新的处置活动。
- 分时入寺：维护时段窗口与按时段、殿堂（含寺院总量层）、入口、人群分层的名额，支持预约确认、持久化候补、取消与确认超时释放、封闭窗口联动收紧，以及可解释的候补递补审计。
- 运营分析：提供寺院、殿堂和香火活动的隐患率、通风利用率、处置成效与可恢复事件游标。
- 身份与审计：提供管理员初始化、用户、角色、会话、权限、操作审计和后台维护能力。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/temple-stewardship.db`。可以复制 `.env.example` 并通过 `TEMPLE_DATABASE_PATH` 指定其他本地路径。

## 数据库初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动 API

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

香火安全、通风处置和修缮协同接口统一使用 `/api/temple` 前缀。

## 测试

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest
```

测试覆盖身份初始化、角色权限、审计脱敏、寺院与殿堂登记、安全策略发布、观测幂等、隐患判定、值守授权、通风容量拒绝、处置完成、固定时钟过期恢复、修缮活动、封闭窗口和分析游标。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 与命令行冒烟

```bash
python -m app.cli smoke
python -m app.cli temple-demo
```

`smoke` 在进程内检查根路径、健康接口和寺务摘要；`temple-demo` 会建立示例寺院、殿堂、香火活动与安全策略，登记值守授权，写入一条异常观测并启动通风处置。

## 目录结构

```text
app/
  temple/          寺院、殿堂、香火活动、观测、隐患、处置、修缮和分析
  api/             用户、角色、认证、审计、系统与维护接口
  core/            时钟、安全、异常、隐私和分页能力
  repositories/    通用 SQLite 查询与身份持久化
  schemas/         身份与管理接口输入模型
  services/        认证、审计、用户、后台任务和维护服务
  cli.py           初始化、检查和业务冒烟入口
  database.py      SQLite 连接、基础表结构与权限初始化
tests/             核心、身份、香火安全、修缮协同和分析回归测试
tools/             本地维护脚本
```

## 数据一致性

SQLite 连接启用外键、WAL、busy timeout 和同步写入策略。策略发布、环境观测与隐患创建、通风资源预留、处置终止、超时恢复和修缮状态变化使用即时事务。值守人员只以脱敏标识参与业务记录，登录令牌仅保存摘要，审计与处置事件不会记录明文密码或令牌。

## 分时入寺预约

接口统一使用 `/api/temple/visits` 前缀。

- `POST /slots`、`GET /slots`、`GET /slots/{id}`：维护时段窗口；详情包含各分层名额的容量、占用与剩余，以及当前候补队列。
- `PUT /slots/{id}/capacities`：按 `hall_code`（省略表示寺院总量层）、`entrance_code`、`visitor_group`（`elder`/`ceremony`/`general`，空串表示全人群）配置分层名额；上调名额会形成 `capacity_adjustment` 释放并立即递补，下调不得低于当前占用。
- `POST /slots/{id}/close`：关闭整个时段，停止新预约并谢绝全部候补。
- `POST /reservations`：创建预约，容量不足时进入持久化候补；支持 `request_id` 幂等重放，同一幂等键对应不同请求体会冲突。
- `POST /reservations/{id}/confirm`、`/cancel`：预约确认与取消，均支持 `request_id` 幂等。
- `POST /reservations/expire-unconfirmed`：扫描确认截止时间，过期占位转为 `expired`、释放名额并递补。
- `POST /closures/apply`：依据已激活的殿堂封闭窗口收紧相关时段：`block_new` 保留存量、`finish_active` 取消未确认占位保留已确认、`cancel_active` 取消全部占位，候补一律谢绝；随后统一递补，重复执行幂等。
- `GET /promotions`：候补递补审计，逐条说明使用的优先规则（`elder_first` 老人团队 → `ceremony_next` 预约法会人员 → `same_entrance` 同入口 → `same_hall` 同殿堂 → `fifo` 先到先得）与对应释放容量（取消、确认超时、封闭窗口或名额上调，含来源预约/窗口编码）。

一致性约定：所有占位（含待确认）都在 `BEGIN IMMEDIATE` 即时事务内按状态实时计数，寺院总量与各分层名额均不会超卖；同一身份在时间重叠的时段不能重复占位；候补名次按叶子队列（时段+殿堂+入口+人群）持久化并重排；取消、确认超时、封闭窗口变化与名额上调都会写入 `capacity_releases` 并在同事务内完成递补，因此请求重放、并发确认与服务重启后，预约状态和候补名次保持一致。

