# storage-btree-torn-write-recovery-validator
验证 B 树页撕裂写、WAL 重放和页版本恢复

## 概述

数据库崩溃后，磁盘上的 B 树页可能只写入了一半（torn page），校验和失败；
WAL 中保存着完整更新，doublewrite 区可能还有旧页副本。由于半页写的
"最后写入时间"不可靠，本验证器不依赖时间戳，而是通过 **校验和 + LSN 链 +
持久化边界** 来判断每个页可安全暴露的版本和重放顺序。

运行测试：

```
python -m unittest
```

要求 Python >= 3.8（开发环境 3.8.2，仅使用标准库）。

## 页格式假设

* 页大小固定为 8192 字节（`PAGE_SIZE`）。
* 页头固定 42 字节，小端序：

  | 偏移 | 字段 | 类型 | 说明 |
  |------|------|------|------|
  | 0  | `page_id` | uint32 | 页号 |
  | 4  | `page_lsn` | uint64 | 该页最后应用的 WAL LSN |
  | 12 | `checksum` | uint32 | CRC32（计算时本字段置零） |
  | 16 | `flags` | uint32 | 页类型标志 |
  | 20 | `left_sibling` | uint32 | 左兄弟页号，`0xFFFFFFFF` 表示无 |
  | 24 | `right_sibling` | uint32 | 右兄弟页号 |
  | 28 | `parent_page` | uint32 | 父页号 |
  | 32 | `parent_pointer_lsn` | uint64 | 父页中指向本页的指针写入时的 LSN |
  | 40 | `slot_count` | uint16 | 槽目录项数 |

* 页头之后是槽目录（uint16 偏移数组），再之后是记录体，其余字节为零填充。
* 校验和覆盖整页；半页写必然导致校验失败，因此 torn page 可可靠检测。
* WAL 帧携带 **整页后像**（full page image），重放天然幂等：同一帧应用
  多次效果相同，重复帧和崩溃恢复中的重复重放都是安全的。
* 每个 WAL 帧记录 `prev_page_lsn`，同一页的帧通过它构成 LSN 链。

## 恢复流程

`RecoveryValidator.recover()` 按以下顺序执行（见
`btree_recovery/validator.py`）：

1. **WAL 截断**：跳过 LSN 大于 `wal_durable_lsn` 的帧（未持久化的尾部）、
   校验和损坏的帧、内容冲突的重复帧；从截断点起的日志全部丢弃。
2. **基页选择**：数据页校验和有效则信任；torn page 回退到 doublewrite
   副本；两者都损坏则该页不可恢复（`failed_pages`）。
3. **逐页重放**：从基页 LSN 出发沿 `prev_page_lsn` 链前进；缺帧或帧内
   页像损坏时链条中断，该页停在中断点，之后的更新不暴露
   （`blocked_updates`）。`replay_state` 中已应用的 LSN 会被跳过，
   保证重复重放幂等。
4. **父子一致性**：若子页的 `parent_pointer_lsn` 超过父页可恢复到的
   LSN，则子页版本回退到该指针 LSN 之下；若基页本身已越界，子页整体
   标记失败，不允许暴露。
5. **兄弟指针修复**：交叉检查左右兄弟指针，单侧丢失时以另一侧为准修复
   并重新计算校验和（`repairs`）。

## 恢复边界（什么不会被暴露）

* LSN 大于 `wal_durable_lsn` 的更新：未满足持久化条件，一律不暴露。
* WAL 缺帧之后的同页更新：链条不完整，停在前一个连续 LSN。
* 父页无法恢复或父页 LSN 落后于子页父指针 LSN 的子页版本。
* 数据页与 doublewrite 副本同时损坏的页：无任何可信版本。
* 冲突重复帧之后的全部日志：视为日志损坏点。

## 输出

`RecoveryReport` 字段：

* `applicable_versions`：每个页可安全暴露的最高 LSN。
* `recovered_pages`：对应的页像（含修复后的兄弟指针）。
* `replay_range` / `replay_order`：全局重放范围与按 LSN 排序的重放顺序。
* `failed_pages`：不可恢复页及原因。
* `blocked_updates`：部分重放被阻止的页及原因。
* `repairs`：修复动作（doublewrite 恢复、兄弟指针修复）。
* `wal_truncated_at`：WAL 截断点 LSN（如有）。

## 目录结构

* `btree_recovery/page.py` — 页格式、校验和、序列化
* `btree_recovery/wal.py` — WAL 帧与帧校验
* `btree_recovery/validator.py` — 恢复验证器主逻辑
* `tests/test_recovery.py` — 半页写、WAL 缺帧/截断、兄弟指针修复、
  重复重放、双重损坏、父子 LSN 不一致等场景
