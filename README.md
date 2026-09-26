# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 身份合并

招生系统偶尔会为同一学员创建两个标识，学时因此分散在不同账号。服务以“身份合并案件”处理：开立案件并提交归属证据，经影响预览确认后批准，系统在别名注册表中追加一个版本化的映射（merged → survivor）。重放查询按指定别名版本把事件聚合到根身份，但每条签到与修正仍保留原始 `student_id`，主键与事件从不改写。发现误合并时撤销案件，注册表追加反向版本，历史版本仍可重放；已签发的冻结快照与案件证据不被改写。合并链（A→B→C）自动解析到根身份，成环或重复占用别名的批准会被拒绝。

相关接口（均位于 `/api/plans/{plan_version}` 下）：

- `POST /identity-merges` 开立案件；`GET /identity-merges`、`GET /identity-merges/{case_id}` 查询
- `POST /identity-merges/{case_id}/evidence` 提交归属证据
- `GET /identity-merges/{case_id}/impact` 影响预览（合并前后对照与批准阻塞项）
- `POST /identity-merges/{case_id}/approve` 批准并生成别名版本
- `POST /identity-merges/{case_id}/revoke` 撤销并生成反向版本
- `GET /identity-aliases` 当前生效映射与完整版本历史
- `GET /snapshot`、`GET /students/{student_id}/progress` 支持 `?alias_version=N` 按版本重放

## 运行方式

默认数据保存在项目目录的 SQLite 文件中。安装依赖后执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，健康检查地址为 `/health`，业务接口位于 `/api`。

## 测试

```bash
python3 -m pytest -q
```

## 编译检查

```bash
python3 -m compileall -q app tests
```

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询，以及身份合并的合并链、循环拒绝、并发事件导入与重启恢复；运行过程中不需要单独的数据库或网络服务。
