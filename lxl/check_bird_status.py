import json
import subprocess
import concurrent.futures

# ================= 配置区 =================
JSON_FILE = "routers.json"
NAMESPACE = "seedemu-k3s-real-topo"
MAX_WORKERS = 20  # 稍微提高点效率
# ==========================================

def check_pod_protocol(pod):
    pod_name = pod.get('pod_name')
    results = []
    
    try:
        # 获取 birdc 状态
        cmd = ["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "birdc", "show", "protocols"]
        stdout = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, timeout=12)
        
        lines = stdout.strip().split('\n')
        for line in lines:
            # 跳过空行和表头
            if not line or any(x in line for x in ['BIRD', 'Name', 'Proto', '---']):
                continue
            
            # 这里的解析逻辑更强健：先按空格分块
            parts = line.split()
            if len(parts) < 4: continue
            
            proto_name = parts[0]   # 具体的协议名，如 Ibgp_to_rr_r2
            proto_type = parts[1]   # BGP, OSPF, etc.
            state = parts[3].lower() # up, down, start
            # 获取最后的信息列内容
            info = " ".join(parts[5:]) if len(parts) > 5 else (parts[4] if len(parts) > 4 else "")

            is_abnormal = False
            
            # 判定逻辑优化：
            # 1. 如果 State 不是 up，必有问题 (OSPF, Direct, Device 等都在这一类)
            if state != "up":
                is_abnormal = True
            
            # 2. 如果是 BGP，除了 State 要是 up，Info 必须是 Established
            if proto_type == "BGP" and "Established" not in info:
                is_abnormal = True

            if is_abnormal:
                results.append({
                    "id": proto_name,
                    "type": proto_type,
                    "state": state,
                    "info": info
                })
                
    except Exception as e:
        results.append({"id": "ERROR", "type": "K8S_EXEC", "state": "failed", "info": str(e)[:50]})

    return pod_name, results

def main():
    print(f"🚀 开始深度巡检 BIRD 状态 (Namespace: {NAMESPACE})...")
    
    try:
        with open(JSON_FILE, 'r') as f:
            pods = json.load(f)
    except FileNotFoundError:
        print(f"❌ 找不到文件: {JSON_FILE}")
        return

    abnormal_pods = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_pod = {executor.submit(check_pod_protocol, p): p for p in pods}
        
        for future in concurrent.futures.as_completed(future_to_pod):
            pod_name, issues = future.result()
            if issues:
                abnormal_pods += 1
                print(f"\n🚨 {pod_name}")
                print(f"{'Protocol ID':<20} {'Type':<8} {'State':<8} {'Extra Info'}")
                print("-" * 65)
                for issue in issues:
                    print(f"{issue['id']:<20} {issue['type']:<8} {issue['state']:<8} {issue['info']}")

    print(f"\n{'='*40}")
    print(f"✅ 巡检报告生成完毕")
    print(f"检查 Pod 总数: {len(pods)}")
    print(f"存在异常的 Pod: {abnormal_pods}")
    print(f"{'='*40}")

if __name__ == "__main__":
    main()