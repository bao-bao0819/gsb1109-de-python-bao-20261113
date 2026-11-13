# 模板调换规则排查复现

TextFSM 状态内规则自上而下、首条命中即停（`textfsm/parser.py` 的 `_CheckLine`）；
模板声明空 `EOF` 状态后，输入结束不再隐式 `Next.Record`（`ParseText`）。

## 文件

- `device_old.template` — 旧模板，宽规则 `^Device ${DeviceID}` 在上，Record 规则被遮蔽
- `device_new.template` — 维护者调换两条 Device 规则，并把内部状态 Detail 改名为 Spec
- `device_rename_only.template` — 规则顺序不变、仅做 Detail→Spec 改名的对照模板
- `field_sample.txt` — 现场回显样本
- `snippet.txt` — 从样本截出的最短复现片段（2 行）
- `verify.py` — 全部断言式核验

## 复现

```
.venv/bin/python scratch/verify.py
.venv/bin/python -m textfsm.parser scratch/device_old.template scratch/snippet.txt
.venv/bin/python -m textfsm.parser scratch/device_new.template scratch/snippet.txt
```
