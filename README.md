# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 身份合并（学员重复建档更正）

招生系统偶尔会为同一学员创建两个学号，学时分散在两个账号下。服务不改写事件主键，而是通过**合并案件**完成更正：

1. `POST /api/plans/{plan}/identity/cases` 登记案件（`survivor_id` 为主身份，`merged_id` 为被合并身份）；
2. `POST .../cases/{case_id}/evidence` 追加归属证据（批准前至少一条）；
3. `GET .../cases/{case_id}/preview` 影响预览：合并前后学时对比、将被重新归属的事件数、不受影响的已签发冻结；
4. `POST .../cases/{case_id}/approve` 批准后追加一个 `merge` 别名版本（自并、重复映射、循环合并会被拒绝）；
5. `POST .../cases/{case_id}/revoke` 发现误合并时追加 `unmerge` 反向版本，历史版本与已签发冻结均不改写。

快照与进度查询支持 `?identity_version=N` 固定在指定身份版本重放：事件按该版本的别名映射聚合到规范身份，每条记录仍保留原始学号（`source_student_ids` 与记录级 `student_id`）。冻结在签发时固定 `identity_version`，之后的合并或撤销都不会改写它。版本时间线见 `GET .../identity/versions`，指定版本的有效映射见 `GET .../identity/versions/{seq}`。

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

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询，以及身份合并的合并链、循环拒绝、并发事件导入、并发批准与重启恢复；运行过程中不需要单独的数据库或网络服务。
