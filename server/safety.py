"""命令安全分级：SAFE（自动执行）/ DANGEROUS（需确认）/ FORBIDDEN（绝对禁止）。"""
import re
from enum import Enum


class RiskLevel(str, Enum):
    SAFE = "safe"
    DANGEROUS = "dangerous"
    FORBIDDEN = "forbidden"


# 绝对禁止的命令模式（不允许确认放行）
FORBIDDEN_PATTERNS = [
    r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+(/|/\*|~|~/|\$HOME)(\s|$)",
    r"\brm\s+-rf\s+/\s*$",
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\s+.*of=/dev/",
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
    r":\(\)\s*\{.*\|.*&.*\}\s*;\s*:",
    r"\bchmod\s+(-[a-zA-Z]+\s+)*777\s+/\s*$",
    r">\s*/dev/(sd|nvme|hd)[a-z0-9]*",
    r"\b(echo|cat|tee)\s+.*>\s*/etc/(passwd|shadow|sudoers)",
    r"\bshutdown\b|\breboot\b|\bhalt\b|\bpoweroff\b",
    r"\bmkswap\b.*\b/dev/",
    r"\bshred\b",
    r"\buserdel\b.*\broot\b",
]

# 危险命令模式（需人工确认）
DANGEROUS_PATTERNS = [
    r"\brm\b",
    r"\brmdir\b",
    r"\bmv\b",
    r"\bdd\b",
    r"\bkill\b",
    r"\bkillall\b",
    r"\bpkill\b",
    r"\bsystemctl\s+(start|stop|restart|enable|disable|mask|unmask|reload|isolate|rescue|emergency)\b",
    r"\bservice\s+\S+\s+(start|stop|restart|reload)\b",
    r"\bmount\b(?!\s+-[tov]\s+\S+\s+\S)(?!\s*$)",
    r"\bmount\s+-[a-zA-Z]*\s+\S",
    r"\bumount\b",
    r"\bchmod\b",
    r"\bchown\b",
    r"\bchgrp\b",
    r"\buseradd\b",
    r"\busermod\b",
    r"\bgroupadd\b",
    r"(^|\s)passwd\s+\S",
    r"\biptables\s+-[AIDRCFZ]\b",
    r"\bnft\s+(add|delete|flush|insert|replace|create|destroy)\b",
    r"\bufw\s+(enable|disable|allow|deny|reject|reset|delete|insert|route)\b",
    r"\bfirewall-cmd\b(?!\s+--(list|query|state|get-)\w*\b)",
    r"\bselinux\b",
    r"\bsetenforce\b",
    # 写入 /etc 敏感文件（但纯读取 cat/grep /etc/passwd 安全）
    r">\s*/etc/|\btee\s+/etc/|\bsed\s+(-[a-zA-Z]+\s+)*-i\b.*\s/etc/",
    r"\btee\b",
    r"\bcrontab\b.*(-r|-e)",
    r"\btruncate\b",
    r"\bmkfs\b",
    r"\bparted\b",
    r"\bfdisk\b",
    r"\bfsck\b",
    r"\bswapoff\b",
    r"\bswapon\b",
    r"\bmodprobe\b",
    r"\brmmod\b",
    r"\bsysctl\s+-w\b",
    r"\bupdate-alternatives\b",
    r"\bapt(-get)?\s+(install|remove|purge|upgrade|dist-upgrade)\b",
    r"\byum\s+(install|remove|update|erase)\b",
    r"\bdnf\s+(install|remove|update|erase)\b",
    r"\bpip\s+install\b",
    r"\bnpm\s+install\b",
    r"\bdocker\s+(rm|rmi|kill|stop|restart|system\s+prune)\b",
    r"\bkubectl\s+(delete|drain|cordon|apply|edit|patch|scale)\b",
    r"\b(git|svn)\s+(push|reset\s+--hard|clean\s+-[a-z]*f)\b",
    r"\bnohup\b",
    r"\bsetsid\b",
    r"\bcurl\b.*\|\s*(ba)?sh\b",
    r"\bwget\b.*\|\s*(ba)?sh\b",
]

# ---------- Windows（cmd / PowerShell）----------
# 绝对禁止：格式化/分区/清盘、删卷影、清日志、关机、递归删盘根
WINDOWS_FORBIDDEN_PATTERNS = [
    # 注意：不能写 \bformat\b —— "Format-Table" 因连字符形成词边界会误伤，必须要求盘符
    r"\bformat(\.com)?\s+[a-zA-Z]:",
    r"\bdiskpart\b",
    r"\bFormat-Volume\b",
    r"\bClear-Disk\b",
    r"\bcipher\s+/w\b",
    r"\bvssadmin\s+delete\s+shadows\b",
    r"\bwevtutil\s+cl\b",
    r"\b(Stop|Restart)-Computer\b",
    r"\bRemove-Item\b[^|;&]*-[a-z]*recurs[a-z]*[^|;&]*[\"']?[a-zA-Z]:\\(?:\*)?[\"']?\s*$",
    r"\brm\b[^|;&]*-[a-z]*r[a-z]*f[a-z]*[^|;&]*[\"']?[a-zA-Z]:[\\/]?(?:\*)?[\"']?\s*$",
    r"\b(rd|rmdir)\s+(/[a-z]\s+)+[\"']?[a-zA-Z]:\\(?:\*)?[\"']?\s*$",
    r"\bdel\s+(?:/[a-z]\s+)+[\"']?[a-zA-Z]:\\(?:\*)?[\"']?\s*$",
]

# 危险（需确认）：Windows 文件删除/服务控制/注册表/权限/安装/计划任务
WINDOWS_DANGEROUS_PATTERNS = [
    # 文件与目录写操作
    r"\bRemove-Item\b",
    r"\b(del|erase)\b",
    r"\b(rd|rmdir)\b",
    r"\b(Move-Item|Rename-Item|Rename-Computer)\b",
    r"\b(Set|New|Remove|Clear)-Item(Property)?\b",
    r"\b(Set|Add|Out)-Content\b",
    r"\bClear-Content\b",
    r"\b(Out-File|Tee-Object|Export-(Csv|Clixml|Console))\b",
    r"\battrib\b",
    # 进程 / 服务
    r"\btaskkill\b",
    r"\b(Start|Stop|Suspend|Resume)-Process\b",
    r"\b(Stop|Restart|Set|Suspend|Resume)-Service\b",
    r"\bnet\s+(stop|start|pause|continue)\s+\S",
    r"\bsc(\.exe)?\s+(config|create|delete|stop|start|failure|sdset)\b",
    # 注册表 / 网络 / 权限 / 用户
    r"\breg\s+(add|delete|copy|restore|import|load|unload|save|compare)\b",
    r"\bnetsh\b",
    r"\b(icacls|cacls|takeown)\b",
    r"\bnet\s+(user|localgroup)\s+\S",
    r"\b(Set|Enable|Disable)-LocalUser\b|\b(Add|Remove)-LocalGroupMember\b",
    # 安装 / 更新 / 引导
    r"\bdism(\.exe)?\b",
    r"\bwusa(\.exe)?\b",
    r"\bmsiexec\b",
    r"\bwinget\s+(install|uninstall|upgrade|update|reset|repair)\b",
    r"\bchoco\s+(install|uninstall|upgrade)\b",
    r"\bbcdedit\b",
    r"\bInitialize-Disk\b|\b(Set|New|Remove)-Partition\b",
    # 计划任务 / 任意代码执行入口
    r"\bschtasks\b",
    r"\bRegister-ScheduledTask\b|\bUnregister-ScheduledTask\b|\bNew-ScheduledTask(Action|Trigger|Settings)\b",
    r"\bStart-Process\b",
    r"\bInvoke-(Expression|Command|CimMethod|WmiMethod|Item|RestMethod|WebRequest)\b|\biex\b|\birm\b",
    r"\b(Invoke|Out)-PowerShell\b",  # 极少见，防御性
    r"\bSet-(MpPreference|MpPreferenceExclusion)\b|\bAdd-MpPreference\b|\bRemove-MpPreference\b",
    r"\bSet-ExecutionPolicy\b",
    r"\bEnable-PSRemoting\b|\bDisable-PSRemoting\b|\bwinrm\b",
    r"\brunas\b",
    r"\bpowershell(\.exe)?\s+(-\w+\s+)*-File\b",  # -File 执行任意脚本
    r"-Enc(odedCommand)?\s|-e[nc]\s+[A-Za-z0-9+/=]{8,}",  # -EncodedCommand 全称与 -ec/-en 缩写
]

# 只读安全：cmd 原生 + PowerShell 查询类（Get-* 按命名约定为只读）
WINDOWS_SAFE_PATTERNS = [
    # cmd 原生
    r"\bdir\b", r"\bver\b", r"\bvol\b", r"\btasklist\b", r"\bsysteminfo\b",
    r"\bdriverquery\b", r"\bipconfig\b", r"\bfindstr\b", r"\bmore\b", r"\bfc\b",
    r"\bchcp\b", r"\bcls\b", r"\bwhere\b",
    r"\broute\s+print\b", r"\barp\s+-a\b", r"\btracert\b", r"\bpathping\b",
    r"\bqwinsta\b", r"\bquery\s+(user|session|termserver)\b",
    r"\bsc(\.exe)?\s+(query\w*|qc|qdescription|qfailure)\b",
    r"\bnet\s+(start|user|statistics|session|use|view|share|account|config)\s*$",
    r"\breg\s+query\b",
    r"\bwevtutil\s+(qe|qpl|ep|el|gp|ge)\b",
    r"\bwmic\s+\S+\s+(get|list)\b",
    # PowerShell 只读（查询类动词按约定不产生副作用）
    r"\bGet-(Content|ChildItem|Item|ItemProperty|Process|Service|ComputerInfo|CimInstance|WmiObject|"
    r"EventLog|WinEvent|NetTCPConnection|NetAdapter|NetIPAddress|NetIPConfiguration|NetRoute|NetNeighbor|"
    r"NetFirewallRule|NetFirewallProfile|PSDrive|Volume|Disk|Partition|PhysicalDisk|Date|Location|Command|"
    r"Alias|Culture|Host|History|FileHash|AuthenticodeSignature|Counter|LocalUser|LocalGroup|"
    r"LocalGroupMember|ScheduledTask|HotFix|WindowsOptionalFeature)\b",
    r"\b(gc|gci|gi|gp|gps|gsv|gwmi|sls|fl|ft|fw)\b",
    r"\b(Select-Object|Select-String|Where-Object|Sort-Object|Group-Object|Compare-Object|Measure-Object)\b",
    r"\bFormat-(Table|List|Wide|Custom)\b",
    r"\bOut-(String|Null|Default|Host)\b",
    r"\bConvertTo-(Json|Csv|Html|Xml)\b|\bConvertFrom-(Json|Csv)\b",
    r"\bWrite-(Output|Host|Verbose|Warning|Debug|Information|Progress)\b",
    r"\b(Test-Path|Test-Connection|Test-NetConnection|Resolve-DnsName)\b",
    r"\b(Set-Location|Push-Location|Pop-Location)\b",
    # 受控的 PowerShell 入口：-Command 载荷必须以 Get- 开头（只读约定）。
    # 管道符/分号会被 _SPLIT_RE 拆成独立片段逐段分级，危险片段仍会被上面的模式拦下。
    r"powershell(\.exe)?\s+(-NoProfile\s+)?-Command\s+\"Get-",
    r"pwsh(\.exe)?\s+(-NoProfile\s+)?-Command\s+\"Get-",
]

# 只读安全命令（白名单前缀）
SAFE_PATTERNS = [
    r"\bls\b", r"\bcat\b", r"\bhead\b", r"\btail\b", r"\bgrep\b", r"\bawk\b", r"\bsed\s+(?!.*-i)\b",
    r"\bps\b", r"\btop\b", r"\bbtop\b", r"\bhtop\b", r"\bdf\b", r"\bdu\b", r"\bfree\b",
    r"\bss\b", r"\bnetstat\b", r"\blsof\b", r"\bip\s+(a|addr|route|link)\b", r"\bifconfig\b",
    r"\bsystemctl\s+(status|is-active|is-enabled|list-units|list-unit-files|show)\b",
    r"\bjournalctl\b", r"\bdmesg\b", r"\blogread\b",
    r"\buptime\b", r"\bwhoami\b", r"\bid\b", r"\bhostname\b", r"\buname\b", r"\bw\b", r"\bwho\b", r"\blast\b",
    r"\bfind\b(?!\s.*(-delete|-exec))", r"\bwc\b", r"\bstat\b", r"\bfile\b", r"\bwhich\b", r"\bwhereis\b",
    r"\bping\b", r"\bdig\b", r"\bnslookup\b", r"\btraceroute\b", r"\btracepath\b",
    r"\bcurl\b(?!\s.*(-o|--output|-O|>\s))", r"\bwget\b(?!\s.*(-O|--output-document|>\s))",
    r"\benv\b", r"\bprintenv\b", r"\bdate\b", r"\bcal\b", r"\bcmd\s+-v\b", r"\btype\b",
    r"\bjq\b", r"\btree\b", r"\bpgrep\b", r"\bpidof\b", r"\blsmod\b", r"\blsusb\b", r"\blspci\b",
    r"\bpython3?\s+(-V|--version)\b", r"\bnode\s+(-v|--version)\b", r"\bjava\s+-version\b",
    r"\becho\b(?!\s.*>)", r"\btrue\b", r"\bfalse\b",
    # docker / k8s 只读子命令（避免被"未知命令"兜底误判）
    r"\bdocker\s+(ps|images|info|inspect|logs|stats|version|top|port|diff|events|search|system\s+df|image\s+ls|container\s+ls|network\s+ls|volume\s+ls|compose\s+ps|compose\s+config)\b",
    r"\bdocker\s+(exec\s+\S+\s+)(ls|cat|head|tail|grep|ps|env|df|free|pwd|uname|whoami|hostname|ip|ifconfig)\b",
    r"\bkubectl\s+(get|describe|logs|top|explain|api-resources|api-versions|config\s+view|version|cluster-info|diff|auth\s+can-i)\b",
    r"\bhelm\s+(list|status|show|get|template|version|history)\b",
    r"\bgit\s+(status|log|diff|show|branch|remote\s+-v|ls-files|tag|blame)\b",
    r"\bsystemctl\s+(status|is-active|is-enabled|list-units|list-unit-files|show|cat)\b",
    r"\bcrontab\s+-l\b", r"\bipvsadm\s+-L\b", r"\bmount\s*$", r"\blsblk\b",
]

_FORBIDDEN_RE = [re.compile(p, re.IGNORECASE)
                 for p in FORBIDDEN_PATTERNS + WINDOWS_FORBIDDEN_PATTERNS]
_DANGEROUS_RE = [re.compile(p, re.IGNORECASE)
                 for p in DANGEROUS_PATTERNS + WINDOWS_DANGEROUS_PATTERNS]
_SAFE_RE = [re.compile(p, re.IGNORECASE)
            for p in SAFE_PATTERNS + WINDOWS_SAFE_PATTERNS]

# 拆分多命令（&& ; | || |）
_SPLIT_RE = re.compile(r"\s*(?:\|\||&&|;|\|)\s*")

# ---------- 结构怀疑检查（白名单命中后的兜底）----------
# 白名单按"前缀/词"匹配，但 $() 子表达式、裸 & 链接、反引号/脱字符转义、
# 换行拼接都能在白名单片段的掩护下执行任意其余代码，且其中出现的命令
# 从未经过"未知→危险"兜底。这些结构一旦出现在 SAFE 片段里，一律降级为危险。
# 注意不做引号豁免：外层引号（如 cmd 传给 powershell 的 -Command "..."）
# 不影响内层 shell 再解析子表达式/调用运算符。
_STRUCT_SUBST_RE = re.compile(r"\$\(|`(?![0abfnrtv\"'`])")  # $(…) 子命令替换；非转义语义的反引号
_STRUCT_AMP_RE = re.compile(r"(?<!>)&(?!>)")                # 裸 &（排除 2>&1 / >&2 / &> 流重定向）
_QSPAN_RE = re.compile(r"\"[^\"]*\"")                       # 双引号包裹段（^ 在其中不生效）


def _suspicious_structure(part: str) -> str:
    """SAFE 片段里的可疑结构描述；无则返回空串。"""
    if _STRUCT_SUBST_RE.search(part):
        return "$() 子命令替换或可疑反引号"
    if _STRUCT_AMP_RE.search(part):
        return "裸 &（命令链接/调用运算符）"
    if "^" in _QSPAN_RE.sub(" ", part):
        return "^ 转义符"
    return ""


def _classify_line(line: str) -> tuple[RiskLevel, str]:
    """单行命令分级：拆分段落后取最严格级别。FORBIDDEN 直接返回。"""
    parts = [p for p in _SPLIT_RE.split(line) if p.strip()] or [line]
    worst = RiskLevel.SAFE
    worst_reason = ""

    for part in parts:
        part = part.strip()
        # 去掉 sudo 前缀再判定
        bare = re.sub(r"^sudo\s+", "", part)

        for rx in _FORBIDDEN_RE:
            if rx.search(bare):
                return RiskLevel.FORBIDDEN, f"绝对禁止的命令: {part[:120]}"

        level = RiskLevel.SAFE
        reason = "只读命令"
        matched_safe = any(rx.search(bare) for rx in _SAFE_RE)
        for rx in _DANGEROUS_RE:
            if rx.search(bare):
                level = RiskLevel.DANGEROUS
                reason = f"危险操作: {part[:120]}"
                break
        if level == RiskLevel.SAFE and not matched_safe:
            level = RiskLevel.DANGEROUS
            reason = f"未知命令（按危险处理）: {part[:120]}"
        if level == RiskLevel.SAFE:
            struct = _suspicious_structure(part)
            if struct:
                level = RiskLevel.DANGEROUS
                reason = f"只读命令中含可疑结构（{struct}）: {part[:120]}"

        if level == RiskLevel.DANGEROUS and worst == RiskLevel.SAFE:
            worst = level
            worst_reason = reason

    return worst, worst_reason or "只读命令"


def classify_command(cmd: str) -> tuple[RiskLevel, str]:
    """返回 (风险级别, 说明)。多命令（含换行多行）拆分后取最严格级别。"""
    cmd = cmd.strip()
    if not cmd:
        return RiskLevel.SAFE, "空命令"

    # 先整串判定绝对禁止模式（防止拆分破坏 fork bomb 等跨行模式）
    for rx in _FORBIDDEN_RE:
        if rx.search(cmd):
            return RiskLevel.FORBIDDEN, f"绝对禁止的命令: {cmd[:120]}"

    # 换行 = PowerShell/cmd/bash 的语句分隔：逐行独立分级，取最严格
    worst = RiskLevel.SAFE
    worst_reason = ""
    for line in re.split(r"[\r\n]+", cmd):
        line = line.strip()
        if not line:
            continue
        level, reason = _classify_line(line)
        if level == RiskLevel.FORBIDDEN:
            return level, reason
        if level == RiskLevel.DANGEROUS and worst == RiskLevel.SAFE:
            worst, worst_reason = level, reason

    return worst, worst_reason or "只读命令"
