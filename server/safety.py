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

_FORBIDDEN_RE = [re.compile(p, re.IGNORECASE) for p in FORBIDDEN_PATTERNS]
_DANGEROUS_RE = [re.compile(p, re.IGNORECASE) for p in DANGEROUS_PATTERNS]
_SAFE_RE = [re.compile(p, re.IGNORECASE) for p in SAFE_PATTERNS]

# 拆分多命令（&& ; | || |）
_SPLIT_RE = re.compile(r"\s*(?:\|\||&&|;|\|)\s*")


def classify_command(cmd: str) -> tuple[RiskLevel, str]:
    """返回 (风险级别, 说明)。多命令拆分后取最严格级别。"""
    cmd = cmd.strip()
    if not cmd:
        return RiskLevel.SAFE, "空命令"

    # 先整串判定绝对禁止模式（防止拆分破坏 fork bomb 等模式）
    for rx in _FORBIDDEN_RE:
        if rx.search(cmd):
            return RiskLevel.FORBIDDEN, f"绝对禁止的命令: {cmd[:120]}"

    parts = [p for p in _SPLIT_RE.split(cmd) if p.strip()]
    if not parts:
        parts = [cmd]

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

        if level.value == "dangerous" and worst.value == "safe":
            worst = level
            worst_reason = reason

    return worst, worst_reason or "只读命令"
