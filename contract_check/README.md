# contract_check — 数据产品兼容性契约核对

发布说明写了"向后兼容"，就用这个工具把它变成可执行的判断：输入前后两个版本的
schema 和读端字段约定，输出承诺是否成立；判成破坏兼容时，给出**最短的合法源记录**
作为反例，使承诺范围内的某个读写组合必然失败。

## 核对模型

**1. 版本号决定承诺范围**（semver，见 `checker.promise_scope`）

| 版本变化 | 承诺 | 承诺覆盖的读写组合 |
|---|---|---|
| major 升位（1.x → 2.0） | 无兼容性承诺 | —（不检查） |
| minor / patch（1.3.0 → 1.4.0 / 1.3.1） | 向后兼容 | `old_write_new_read` 且 `new_write_old_read` |

**2. 字段约定**

- 每个字段声明 `required`（必填/可选）与类型；`enum` 字段列出全部合法值。
- 读端两种约定（`--reader`）：
  - `strict`：拒收含未知字段的记录；
  - `lenient`：忽略未知字段。
- **未知枚举值任何读端都拒收**——读端无法把它映射到代码分支，这与字段约定无关。

**3. 读写组合逐向判定**（`checker.check_direction`）

| 违例 | 触发条件 | 失败方向 |
|---|---|---|
| `unknown_enum_value` | 写端枚举值 ⊄ 读端枚举值 | 枚举扩位 → `new_write_old_read`；枚举收窄 → `old_write_new_read` |
| `unknown_field` | 写端有读端未声明的字段，且读端 strict | 新增字段 → `new_write_old_read` |
| `missing_required_field` | 读端必填字段在写端 schema 中不存在 | 新增必填 → `old_write_new_read` |
| `required_tightened` | 写端可选、读端必填 | 可选改必填 → `old_write_new_read` |

**4. 反例记录（最短合法源记录）**

每条违例附带 `record`：只含写端必填字段、取各类型最小合法值（枚举取首值、
字符串取 `""`、整数取 `0`、布尔取 `false`），再加上触发拒收的最小改动（一个新
枚举值，或一个新增字段）。它对写端合法、被读端拒收，可直接放进回归测试。

## 使用

```bash
# 从仓库根目录运行；退出码 0 = 承诺成立/无承诺，1 = 判定破坏兼容
.venv/bin/python -m contract_check OLD.json NEW.json [--reader strict|lenient] [--json]
.venv/bin/python -m pytest contract_check/tests -o addopts=""
```

schema 文件格式见 `contract_check/examples/`。

## 示例结论

- `orders 1.3.0 → 1.4.0`（枚举新增 `archived`，minor 升位）：**broken**。
  反例 `{"id": "", "status": "archived"}` 在 `new_write_old_read` 失败——
  新写端合法产出，旧读端不认识该枚举值。要么改发 major，要么旧读端先学会
  未知枚举的兜底分支。
- `profile 2.0.0 → 2.1.0`（新增可选字段 `nickname`）：strict 读端 **broken**
  （反例 `{"id": "", "email": "", "nickname": ""}`）；lenient 读端 **kept**。
  同一个变更是否破坏兼容，取决于读端字段约定——发布说明里的"向后兼容"必须
  注明它承诺的是哪种读端。
