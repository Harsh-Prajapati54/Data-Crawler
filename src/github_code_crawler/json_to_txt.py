import json

input_file = r"data\train.jsonl"
output_file = r"data\code.txt"

FILE_END = "<|file_end|>"

with open(input_file, "r", encoding="utf-8") as infile, \
open(output_file, "w", encoding="utf-8") as outfile:

    for line_number, line in enumerate(infile, start=1):
        line = line.strip()

        if not line:
            continue

        try:
            data = json.loads(line)
            code = data.get("text", "")

            if code:
                outfile.write(code)
                outfile.write("\n")
                outfile.write(FILE_END)
                outfile.write("\n\n")

        except json.JSONDecodeError:
            print(f"Skipping invalid JSON at line {line_number}")

            print(f"Code extracted to {output_file}")