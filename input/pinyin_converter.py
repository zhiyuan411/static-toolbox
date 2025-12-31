#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
拼音词库格式转换脚本
功能：读取外部txt文件中的原始拼音词库，转换为前端可直接使用的JSON结构化字典
使用：
  1. 赋予执行权限：chmod +x pinyin_converter.py
  2. 执行方式：
     - 默认：./pinyin_converter.py （读取 input_pinyin.txt，输出 pinyin_dict.json）
     - 自定义：./pinyin_converter.py 输入文件.txt 输出文件.json
"""
import re
import json
import sys
import ast

def load_raw_data_from_txt(input_file):
    """
    从txt文件读取原始词库数据并解析为Python列表
    :param input_file: 输入txt文件路径
    :return: 解析后的原始词库列表
    """
    try:
        with open(input_file, "r", encoding="utf-8") as f:
            # 读取文件内容并清理首尾空白（换行/空格）
            raw_content = f.read().strip()
            # 安全解析字符串为Python列表（替代eval，避免安全风险）
            raw_code_table = ast.literal_eval(raw_content)
            
            # 验证解析结果是否为列表
            if not isinstance(raw_code_table, list):
                raise ValueError("文件内容不是合法的列表格式")
            
            # 过滤空值
            raw_code_table = [item.strip() for item in raw_code_table if item.strip()]
            print(f"[成功] 读取输入文件：{input_file}")
            print(f"[成功] 共读取{len(raw_code_table)}条原始词库数据")
            return raw_code_table
    
    except FileNotFoundError:
        print(f"[错误] 输入文件不存在：{input_file}", file=sys.stderr)
        sys.exit(1)
    except SyntaxError:
        print(f"[错误] 输入文件格式错误（非合法Python列表）：{input_file}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"[错误] 输入文件内容验证失败：{e}", file=sys.stderr)
        sys.exit(1)
    except IOError as e:
        print(f"[错误] 读取文件失败：{e}", file=sys.stderr)
        sys.exit(1)

def convert_pinyin_dict(raw_code_table, output_file):
    """
    将原始拼音词库转换为JSON格式的结构化字典
    :param raw_code_table: 解析后的原始词库列表
    :param output_file: 输出JSON文件路径
    """
    # 初始化结构化字典
    pinyin_dict = {}
    
    # 正则匹配规则：提取开头的拼音（字母+单引号）和后续的候选字符串
    pattern = re.compile(r'^([a-z\']+)(.*)$', re.IGNORECASE)
    
    # 遍历解析每个条目
    error_count = 0
    for idx, item in enumerate(raw_code_table):
        match = pattern.match(item)
        if not match:
            print(f"[警告] 第{idx}条数据格式错误（{item}），已跳过", file=sys.stderr)
            error_count += 1
            continue
        
        # 提取拼音和候选字符串
        pinyin = match.group(1).lower()  # 统一转为小写，避免大小写问题
        candidates_str = match.group(2).strip()
        
        # 拆分候选词/字（按逗号分割，过滤空值）
        candidates = [c.strip() for c in candidates_str.split(',') if c.strip()]
        
        # 存入字典（避免重复拼音覆盖，若有重复可合并候选）
        if pinyin in pinyin_dict:
            # 去重后合并候选（保留首次出现顺序）
            existing = set(pinyin_dict[pinyin])
            new_candidates = [c for c in candidates if c not in existing]
            pinyin_dict[pinyin].extend(new_candidates)
        else:
            pinyin_dict[pinyin] = candidates
    
    # 写入JSON文件
    try:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(
                pinyin_dict,
                f,
                ensure_ascii=False,  # 保留中文原样
                indent=2,           # 格式化缩进，便于阅读
                sort_keys=True      # 按拼音排序，便于维护
            )
        print(f"\n[成功] 转换完成！")
        print(f"[成功] 有效解析{len(pinyin_dict)}个拼音词条（跳过{error_count}条错误数据）")
        print(f"[成功] 输出文件：{output_file}")
    except IOError as e:
        print(f"[错误] 写入输出文件失败：{e}", file=sys.stderr)
        sys.exit(1)

# ===================== 主执行逻辑 =====================
if __name__ == "__main__":
    # 命令行参数处理
    # 用法1: ./pinyin_converter.py → 输入input_pinyin.txt，输出pinyin_dict.json
    # 用法2: ./pinyin_converter.py my_input.txt my_output.json
    input_file = sys.argv[1] if len(sys.argv) >= 2 else "input_pinyin.txt"
    output_file = sys.argv[2] if len(sys.argv) >= 3 else "pinyin_dict.json"
    
    # 1. 读取并解析输入txt文件
    raw_code_table = load_raw_data_from_txt(input_file)
    
    # 2. 转换为JSON结构化字典
    convert_pinyin_dict(raw_code_table, output_file)
