#!/usr/bin/env python3
"""
EVM Trace 解析器 - 完整版本
支持从 RPC 获取 trace 数据，解析合约 ABI，计算函数签名
"""

import json
import requests
import time
import os
import argparse
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
from eth_abi import decode
from eth_utils import to_checksum_address, keccak
import re
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

@dataclass
class FunctionInfo:
    """函数信息"""
    name: str
    signature: str
    input_types: List[str]
    output_types: List[str]
    input_names: List[str]
    output_names: List[str]

@dataclass
class ContractInfo:
    """合约信息"""
    address: str
    name: str
    functions: Dict[str, FunctionInfo]  # signature -> FunctionInfo
    abi: str

class EVMTraceParser:
    def __init__(self, etherscan_api_key: Optional[str] = None):
        # 从环境变量获取 API 密钥，如果参数提供则优先使用参数
        self.etherscan_api_key = etherscan_api_key or os.getenv('ETHERSCAN_API_KEY')
        if not self.etherscan_api_key:
            raise ValueError("请提供 Etherscan API 密钥，通过参数或环境变量 ETHERSCAN_API_KEY")
        
        self.contract_cache: Dict[str, ContractInfo] = {}
        
        # 从环境变量读取 RPC 接口配置
        self.rpc_urls = {
            "ETH": os.getenv('RPC_ETH', ""),
            "BSC": os.getenv('RPC_BSC', ""),
            "POLYGON": os.getenv('RPC_POLYGON', "")
        }
        
        # 链 ID 映射
        self.chain_ids = {
            "ETH": 1,
            "BSC": 56,
            "POLYGON": 137
        }
        
        # 从环境变量读取请求控制参数
        self.request_delay = float(os.getenv('REQUEST_DELAY', '0.2'))
        self.last_request_time = 0
        self.max_retries = int(os.getenv('MAX_RETRIES', '3'))
        self.timeout = int(os.getenv('REQUEST_TIMEOUT', '30'))
        
        # 从环境变量读取缓存和输出配置
        self.enable_cache = os.getenv('ENABLE_CACHE', 'true').lower() == 'true'
        self.cache_dir = os.getenv('CACHE_DIR', 'source_code')
        self.output_dir = os.getenv('OUTPUT_DIR', 'docs')
    
    def _wait_for_rate_limit(self):
        """控制请求频率"""
        current_time = time.time()
        time_since_last = current_time - self.last_request_time
        if time_since_last < self.request_delay:
            time.sleep(self.request_delay - time_since_last)
        self.last_request_time = time.time()
    
    def _make_etherscan_request(self, params: Dict) -> Optional[Dict]:
        """带重试的 Etherscan API 请求"""
        url = "https://api.etherscan.io/v2/api"
        
        for attempt in range(self.max_retries):
            try:
                self._wait_for_rate_limit()
                print(f"请求合约信息: {params.get('address', '')} (尝试 {attempt + 1}/{self.max_retries})")
                
                response = requests.get(url, params=params, timeout=self.timeout)
                response.raise_for_status()
                
                data = response.json()
                
                if data.get("status") == "1":
                    return data
                elif data.get("status") == "0":
                    error_msg = data.get("message", "Unknown error")
                    if "rate limit" in error_msg.lower():
                        print(f"遇到速率限制，等待更长时间...")
                        time.sleep(1.0 * (attempt + 1))  # 指数退避
                        continue
                    else:
                        print(f"API 错误: {error_msg}")
                        return None
                
            except requests.exceptions.Timeout:
                print(f"请求超时 (尝试 {attempt + 1}/{self.max_retries})")
                if attempt < self.max_retries - 1:
                    time.sleep(2.0 * (attempt + 1))  # 指数退避
                    continue
            except requests.exceptions.RequestException as e:
                print(f"请求失败: {e} (尝试 {attempt + 1}/{self.max_retries})")
                if attempt < self.max_retries - 1:
                    time.sleep(1.0 * (attempt + 1))
                    continue
            except Exception as e:
                print(f"未知错误: {e}")
                break
        
        print(f"获取合约信息失败: {params.get('address', '')}")
        return None
    
    def get_trace_from_rpc(self, chain: str, tx_hash: str) -> Dict:
        """通过 RPC 获取 trace 数据"""
        if chain not in self.rpc_urls:
            raise ValueError(f"不支持的链: {chain}")
        
        url = self.rpc_urls[chain]
        payload = {
            "method": "trace_transaction",
            "params": [tx_hash],
            "id": 1,
            "jsonrpc": "2.0"
        }
        
        headers = {'Content-Type': 'application/json'}
        
        try:
            print(f"从 RPC 获取 trace 数据...")
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            if "error" in data:
                raise Exception(f"RPC 错误: {data['error']}")
            
            return data
            
        except Exception as e:
            raise Exception(f"获取 trace 数据失败: {e}")
    
    def save_trace_to_file(self, trace_data: Dict, output_file: Optional[str] = None):
        """保存 trace 数据到文件"""
        if output_file is None:
            output_file = f"{self.output_dir}/trace.json"
            
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(trace_data, f, indent=2, ensure_ascii=False)
    
    def get_contract_info(self, address: str, chain: str) -> Optional[ContractInfo]:
        """获取合约信息"""
        address = address.lower()
        
        if address in self.contract_cache:
            return self.contract_cache[address]
        
        # 检查是否为EOA地址（通常以0x开头且长度为42）
        if not address.startswith('0x') or len(address) != 42:
            return None
        
        try:
            chain_id = self.chain_ids.get(chain, 1)
            params = {
                "chainid": chain_id,
                "module": "contract",
                "action": "getsourcecode",
                "address": address,
                "apikey": self.etherscan_api_key
            }
            
            data = self._make_etherscan_request(params)
            
            if not data or not data.get("result"):
                return None
            
            result = data["result"][0]
            contract_name = result.get("ContractName", "")
            source_code = result.get("SourceCode", "")
            abi_str = result.get("ABI", "")
            
            if not contract_name:
                # 合约未验证，缓存为None避免重复请求
                self.contract_cache[address] = None
                return None
            
            # 保存源代码
            if source_code:
                self.save_source_code(contract_name, source_code)
            
            # 解析 ABI
            functions = {}
            if abi_str and abi_str != "Contract source code not verified":
                try:
                    abi = json.loads(abi_str)
                    functions = self.parse_abi(abi)
                    print(f"成功解析 ABI: {contract_name} ({len(functions)} 个函数)")
                except Exception as e:
                    print(f"解析 ABI 失败 {address}: {e}")
            
            contract_info = ContractInfo(
                address=address,
                name=contract_name,
                functions=functions,
                abi=abi_str
            )
            
            self.contract_cache[address] = contract_info
            print(f"成功获取合约信息: {contract_name}")
            return contract_info
            
        except Exception as e:
            print(f"获取合约信息异常 {address}: {e}")
            return None
    
    def save_source_code(self, contract_name: str, source_code: str):
        """保存源代码到文件"""
        if not self.enable_cache:
            return
        
        os.makedirs(self.cache_dir, exist_ok=True)
        
        # 清理文件名
        safe_name = re.sub(r'[^\w\-_\.]', '_', contract_name)
        filename = f"{self.cache_dir}/{safe_name}.txt"
        
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(source_code)
        print(f"保存源代码: {filename}")
    
    def parse_abi(self, abi: List[Dict]) -> Dict[str, FunctionInfo]:
        """解析 ABI 并计算函数签名"""
        functions = {}
        
        for item in abi:
            if item.get("type") == "function":
                name = item.get("name", "")
                inputs = item.get("inputs", [])
                outputs = item.get("outputs", [])
                
                # 构建函数签名
                input_types = [inp.get("type", "") for inp in inputs]
                input_names = [inp.get("name", "") for inp in inputs]
                output_types = [out.get("type", "") for out in outputs]
                output_names = [out.get("name", "") for out in outputs]
                
                # 计算函数签名哈希
                signature_string = f"{name}({','.join(input_types)})"
                signature_hash = keccak(signature_string.encode()).hex()
                signature = "0x" + signature_hash[:8]
                
                function_info = FunctionInfo(
                    name=name,
                    signature=signature,
                    input_types=input_types,
                    output_types=output_types,
                    input_names=input_names,
                    output_names=output_names
                )
                
                functions[signature] = function_info
        
        return functions
    
    def get_function_signature(self, input_data: str) -> str:
        """从 input 数据中提取函数签名"""
        if not input_data or input_data == "0x":
            return "<empty>"
        
        if len(input_data) >= 10:
            return input_data[:10]
        else:
            return input_data
    
    def get_contract_name(self, address: str, chain: str) -> str:
        """获取合约名称"""
        contract_info = self.get_contract_info(address, chain)
        if contract_info:
            return contract_info.name
        return address
    
    def get_function_name(self, address: str, input_data: str, chain: str) -> str:
        """获取函数名称"""
        signature = self.get_function_signature(input_data)
        
        if signature == "<empty>":
            return "<empty>"
        
        contract_info = self.get_contract_info(address, chain)
        if contract_info and signature in contract_info.functions:
            return contract_info.functions[signature].name
        
        return signature
    
    def decode_input_data(self, address: str, input_data: str, chain: str) -> List[Tuple[str, str, Any]]:
        """解码输入数据"""
        if not input_data or input_data == "0x":
            return []
        
        signature = self.get_function_signature(input_data)
        contract_info = self.get_contract_info(address, chain)
        
        if not contract_info or signature not in contract_info.functions:
            return []
        
        function_info = contract_info.functions[signature]
        
        try:
            # 获取参数数据（去掉函数签名）
            param_data = input_data[10:]
            if not param_data or not function_info.input_types:
                return []
            
            # 解码参数
            decoded = decode(function_info.input_types, bytes.fromhex(param_data))
            
            result = []
            for i, (param_type, param_name, value) in enumerate(zip(
                function_info.input_types, 
                function_info.input_names, 
                decoded
            )):
                if not param_name:
                    param_name = f"param{i}"
                
                result.append((param_name, param_type, value))
            
            return result
            
        except Exception as e:
            print(f"解码输入数据失败: {e}")
            return []
    
    def decode_output_data(self, address: str, input_data: str, output_data: str, chain: str) -> List[Tuple[str, str, Any]]:
        """解码输出数据"""
        if not output_data or output_data == "0x":
            return []
        
        signature = self.get_function_signature(input_data)
        contract_info = self.get_contract_info(address, chain)
        
        if not contract_info or signature not in contract_info.functions:
            return []
        
        function_info = contract_info.functions[signature]
        
        try:
            if not function_info.output_types:
                return []
            
            # 解码输出
            decoded = decode(function_info.output_types, bytes.fromhex(output_data[2:]))
            
            result = []
            for i, (param_type, param_name, value) in enumerate(zip(
                function_info.output_types,
                function_info.output_names,
                decoded
            )):
                if not param_name:
                    param_name = f"output{i}"
                
                result.append((param_name, param_type, value))
            
            return result
            
        except Exception as e:
            print(f"解码输出数据失败: {e}")
            return []
    
    def format_value(self, value: Any, value_type: str, chain: str) -> str:
        """格式化参数值"""
        if value_type == "address":
            addr_str = to_checksum_address(value) if isinstance(value, str) else str(value)
            return self.get_contract_name(addr_str, chain)
        elif value_type.startswith("uint") or value_type.startswith("int"):
            if isinstance(value, int):
                return f"{value:,}"
            else:
                return str(value)
        elif value_type == "bool":
            return str(value).lower()
        elif value_type.startswith("bytes"):
            if isinstance(value, bytes):
                return "0x" + value.hex()
            return str(value)
        elif value_type.endswith("[]"):
            # 数组类型
            if isinstance(value, (list, tuple)):
                element_type = value_type[:-2]
                formatted_elements = [self.format_value(elem, element_type, chain) for elem in value]
                return f"[{', '.join(formatted_elements)}]"
            return str(value)
        else:
            return str(value)
    
    def format_parameters(self, params: List[Tuple[str, str, Any]], chain: str) -> str:
        """格式化参数列表"""
        if not params:
            return ""
        
        formatted_params = []
        for param_name, param_type, param_value in params:
            formatted_value = self.format_value(param_value, param_type, chain)
            formatted_params.append(f"{param_name}={formatted_value}")
        
        return ", ".join(formatted_params)
    
    def wei_to_ether(self, wei_value: str) -> float:
        """将 Wei 转换为 Ether"""
        if wei_value.startswith("0x"):
            wei_int = int(wei_value, 16)
        else:
            wei_int = int(wei_value)
        return wei_int / (10 ** 18)
    
    def format_trace_item(self, trace_item: Dict, level: int, chain: str, include_static_call: bool = True) -> Optional[str]:
        """格式化单个 trace 项"""
        action = trace_item.get("action", {})
        result = trace_item.get("result", {})
        
        call_type = action.get("callType", "").upper()
        to_address = action.get("to", "")
        value = action.get("value", "0x0")
        input_data = action.get("input", "")
        output_data = result.get("output", "")
        
        # 处理 CREATE 类型的 trace
        if trace_item.get("type") == "create":
            call_type = "CREATE"
            to_address = result.get("address", "")  # 新创建的合约地址
        
        # 如果不包含 STATICCALL 且当前是 STATICCALL，则跳过
        if not include_static_call and call_type == "STATICCALL":
            return None
        
        # 格式化层级
        level_str = "  " * level
        
        # 格式化 Value
        value_str = ""
        if value != "0x0" and value != "0":
            ether_value = self.wei_to_ether(value)
            if ether_value > 0:
                value_str = f"[value: {ether_value} Ether] "
        
        # 获取合约名称和函数名称
        contract_name = self.get_contract_name(to_address, chain)
        function_name = self.get_function_name(to_address, input_data, chain)
        
        # 解码输入和输出参数
        input_params = self.decode_input_data(to_address, input_data, chain)
        output_params = self.decode_output_data(to_address, input_data, output_data, chain)
        
        # 格式化参数
        input_str = self.format_parameters(input_params, chain)
        output_str = self.format_parameters(output_params, chain)
        
        # 构建最终字符串
        function_call = f"{contract_name}.{function_name}({input_str})"
        output_part = f" => ({output_str})" if output_str else " => ()"
        
        result_str = f"{level_str}{level} [{call_type}] {value_str}{function_call}{output_part}"
        return result_str
    
    def parse_trace(self, trace_data: Dict, chain: str, include_static_call: bool = True) -> List[str]:
        """解析完整的 trace 数据"""
        result = trace_data.get("result", [])
        
        if not result:
            return ["没有找到 trace 数据"]
        
        formatted_lines = []
        
        # 处理每个 trace 项
        for i, trace_item in enumerate(result):
            trace_address = trace_item.get("traceAddress", [])
            level = len(trace_address)
            
            # 添加发送者信息（仅第一个 trace）
            if i == 0:
                action = trace_item.get("action", {})
                from_address = action.get("from", "")
                if from_address:
                    sender_name = self.get_contract_name(from_address, chain)
                    formatted_lines.append(f"[Sender] {sender_name}")
            
            formatted_line = self.format_trace_item(trace_item, level, chain, include_static_call)
            if formatted_line is not None:  # 只添加非 None 的行
                formatted_lines.append(formatted_line)
        
        return formatted_lines
    
    def parse_local_trace_file(self, input_file: Optional[str] = None, chain: str = "BSC", include_static_call: bool = True) -> str:
        """解析本地 trace 文件"""
        try:
            if input_file is None:
                input_file = f"{self.output_dir}/trace.json"
                
            with open(input_file, 'r', encoding='utf-8') as f:
                trace_data = json.load(f)
            
            print(f"正在解析本地 trace 文件: {input_file}")
            formatted_lines = self.parse_trace(trace_data, chain, include_static_call)
            return "\n".join(formatted_lines)
            
        except Exception as e:
            return f"解析文件时出错: {e}"
    
    def save_to_file(self, content: str, output_dir: Optional[str] = None) -> str:
        """保存结果到文件"""
        if output_dir is None:
            output_dir = self.output_dir
            
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"readableTrace{timestamp}.txt"
        filepath = os.path.join(output_dir, filename)
        
        os.makedirs(output_dir, exist_ok=True)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        
        return filepath
    
    def process_transaction(self, chain: str, tx_hash: str, include_static_call: bool = True) -> str:
        """处理完整的交易流程"""
        print(f"正在获取 {chain} 链上交易 {tx_hash} 的 trace 数据...")
        
        # 1. 获取 trace 数据
        trace_data = self.get_trace_from_rpc(chain, tx_hash)
        
        # 2. 保存 trace 数据
        self.save_trace_to_file(trace_data)
        print("trace 数据已保存到 docs/trace.json")
        
        # 3. 解析 trace 数据
        print("正在解析 trace 数据...")
        formatted_lines = self.parse_trace(trace_data, chain, include_static_call)
        result = "\n".join(formatted_lines)
        
        # 4. 保存解析结果
        output_file = self.save_to_file(result)
        print(f"解析结果已保存到: {output_file}")
        
        return result

def main():
    """主函数"""
    # 解析命令行参数
    arg_parser = argparse.ArgumentParser(description='EVM Trace 解析器')
    arg_parser.add_argument('-s', '--static-call', action='store_true', 
                           help='包含 STATICCALL 信息（默认：不包含）')
    arg_parser.add_argument('-c', '--chain', default='BSC', 
                           help='区块链名称（默认：BSC）')
    arg_parser.add_argument('-t', '--tx', 
                           help='交易哈希（如果未提供将使用本地 trace.json 文件）')
    
    args = arg_parser.parse_args()
    
    # 创建解析器实例（API 密钥从环境变量读取）
    try:
        parser = EVMTraceParser()
    except ValueError as e:
        print(f"配置错误: {e}")
        print("请确保设置了环境变量 ETHERSCAN_API_KEY 或创建 .env 文件")
        return
    
    # 输出配置信息
    static_call_status = "包含" if args.static_call else "不包含"
    print(f"配置: 链={args.chain}, STATICCALL={static_call_status}")
    
    # 检查是否提供了交易哈希
    if args.tx:
        print(f"正在处理交易: {args.tx}")
        try:
            # 处理交易
            result = parser.process_transaction(args.chain, args.tx, args.static_call)
            
            # 显示部分结果
            print("\n解析结果预览:")
            print("=" * 50)
            lines = result.split('\n')
            for i, line in enumerate(lines[:15]):
                print(line)
            if len(lines) > 15:
                print(f"... 还有 {len(lines) - 15} 行")
                
        except Exception as e:
            print(f"处理失败: {e}")
    else:
        # 检查是否存在本地 trace.json 文件
        trace_file = f"{parser.output_dir}/trace.json"
        if os.path.exists(trace_file):
            print("发现本地 trace.json 文件，将进行解析...")
            try:
                # 解析本地文件
                result = parser.parse_local_trace_file(None, args.chain, args.static_call)
                
                # 保存结果
                output_file = parser.save_to_file(result)
                print(f"解析完成！结果已保存到: {output_file}")
                
                # 显示部分结果
                print("\n解析结果预览:")
                print("=" * 50)
                lines = result.split('\n')
                for i, line in enumerate(lines[:15]):  # 显示前15行
                    print(line)
                if len(lines) > 15:
                    print(f"... 还有 {len(lines) - 15} 行")
                    
            except Exception as e:
                print(f"解析失败: {e}")
        else:
            print("未发现本地 trace.json 文件")
            print("请使用 -t/--tx 参数提供交易哈希，或确保 docs/trace.json 文件存在")
            print("使用示例:")
            print("  python3 main.py -s")
            print("  python3 main.py -t 0x2d9c1a00cf3d2fda268d0d11794ad2956774b156355e16441d6edb9a448e5a99")
            print("  python3 main.py -s -c BSC -t 0x2d9c1a00cf3d2fda268d0d11794ad2956774b156355e16441d6edb9a448e5a99")

if __name__ == "__main__":
    main()
