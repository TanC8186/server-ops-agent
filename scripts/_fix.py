import io

p = 'scripts/normalize_corpus.py'
lines = io.open(p, encoding='utf-8', newline='').read().split('\n')
# 定位被破坏的三行注释 + 换行参数（296..301，1-based）
start = None
for index, line in enumerate(lines):
    if '必须固定 LF' in line:
        start = index
        break
assert start is not None, 'anchor not found'
# 找到 out_path.write_text 结束（')' 结束行）
end = None
for index in range(start, start + 12):
    if lines[index].strip() == ')':
        end = index
        break
assert end is not None, 'write_text end not found'

replacement = [
    '    # 必须固定 LF：manifest 的 normalized_sha256 是按 LF 字节算的，Windows 上',
    '    # 默认 newline 会把 \\n 落成 \\r\\n，直接对磁盘字节校验就会失败。',
    '    out_path.write_text(markdown, encoding="utf-8", newline="\\n")',
]
lines[start:end + 1] = replacement
io.open(p, 'w', encoding='utf-8', newline='\n').write('\n'.join(lines))
print('repaired lines', start, end)
