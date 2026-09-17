import sys

STEP_PATH = sys.argv[1]

from OCC.Extend.DataExchange import read_step_file_with_names_colors

print("调用 read_step_file_with_names_colors ...")
result = read_step_file_with_names_colors(STEP_PATH)
print(f"共返回 {len(result)} 个条目\n")

for i, (shape, (name, color)) in enumerate(result.items()):
    print(f"[{i}] name={name!r}")
    if i >= 30:
        print("... (truncated)")
        break
