namespace StardewAI.Companion.Mod.Menus;

public static class NativeChatText
{
    public static string? ReplyTarget(string? focused, IEnumerable<string> pending)
    {
        var ids = pending.Distinct().ToArray();
        // An old focus must never silently retarget a reply to a newer question.
        if (focused != null) return ids.Contains(focused) ? focused : null;
        return ids.Length == 1 ? ids[0] : null;
    }

    public static IEnumerable<string> Segments(string text)
    {
        // ChatBox wraps a complete message itself. Splitting by measured width
        // creates separate history entries and repeats the speaker on every line.
        string clean = CompanionDialogueInbox.PlainText(text).Replace('[', '［').Replace(']', '］').Trim();
        if (clean.Length > 0) yield return clean;
    }
}
