using System.Text.RegularExpressions;

namespace StardewAI.Companion.Mod.Domain;

/// <summary>Recognizes imperative controls for admission while a conversation is pending.
/// The runtime remains the authority for scope and cancellation.</summary>
public static class WorkControlDirective
{
    public static bool IsPriority(string text)
    {
        text = text.Trim().TrimEnd('。', '！', '!', ' ');
        if (text is "暂停" or "停下" or "停止" or "先停下" or "先暂停" or "继续" or "恢复" or "继续工作" or "恢复工作") return true;
        if (Regex.IsMatch(text, "[？?\"“”‘’]|(?:如果|假如|是否|什么意思|怎么|如何|不要取消|别取消|不要撤销)")) return false;
        var clauses = Regex.Split(text, "[，,；;。]");
        var match = Regex.Match(clauses[0].Trim(), "^(?:请)?(?:取消|撤销)(.*)$");
        if (!match.Success || clauses.Skip(1).Where(c => c.Trim().Length > 0).Any(c => !Regex.IsMatch(c.Trim(), "^(?:请)?(?:立即|马上|现在)?(?:停止|停下|不再派工|不要再派工|不要继续|不再执行)$"))) return false;
        string target = match.Groups[1].Value.Trim();
        return target.Length == 0 || target is "任务" or "工作" or "安排" or "计划"
            || Regex.IsMatch(target, "^(?:所有|全部|一切)(?:的)?(?:任务|工作|安排|计划)$")
            || Regex.IsMatch(target, "^(?:刚才|刚刚|当前|现在|这)(?:的|项|个)?[^，,；;。？?]{0,20}(?:任务|工作|安排|计划)$");
    }
}
