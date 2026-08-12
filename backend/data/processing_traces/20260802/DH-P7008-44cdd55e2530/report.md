# 文档处理报告：DH-P7008.txt

## 概览
- 状态：失败
- 文件：DH-P7008.txt
- 来源路径：uploads/2394106ae7554c3fb06c13dbd9539a9f/upload-30b9dc75e54b4588957fd66be527ac43/DH-P7008.txt
- 文档 ID：bc27c2647438a4b64e5ef8097b541a48
- Trace ID：44cdd55e25304a4b9d6334796f10e592
- 开始时间：2026-08-02T15:27:00.890Z
- 结束时间：2026-08-02T15:27:01.149Z
- 总耗时：262 ms
- 文件大小：3.3 KB
- 文件类型：.txt

## 阶段时间线
| 阶段 | 状态 | 耗时 | 关键结果 |
|---|---:|---:|---|
| 文档加载 / 解析 | 失败 | 54 ms | database is locked |

## 关键配置
- 解析引擎：builtin
- 切片策略：auto
- 父块大小：4096 字符
- 子块大小：384 字符
- 子块重叠：76 字符
- Dense 检索：开启
- Keyword 检索：开启
- OCR：关闭
- 多模态：关闭

## 产物说明
- `report.md`：当前这份面向人工排查的总览报告。
- `parsed.md`：解析器抽取后的正文，适合检查原文是否读对、是否乱码。
- `chunks_preview.md`：前若干个切片的可读预览，适合快速确认切片质量。
- `chunks.jsonl`：完整切片明细，适合程序读取或深度排查。
- `trace.json`：完整机器 trace，供前端 trace 抽屉和自动化分析使用。

## 错误
- 类型：OperationalError
- 信息：database is locked

```text
Traceback (most recent call last):
  File "D:\python_project\new-rag-project\backend\app\services\retrieval\rag_service.py", line 507, in parse_and_index_document
    self.document_repository.upsert_document(
  File "D:\python_project\new-rag-project\backend\app\services\documents\document_repository.py", line 61, in upsert_document
    conn.execute(
sqlite3.OperationalError: database is locked
```
