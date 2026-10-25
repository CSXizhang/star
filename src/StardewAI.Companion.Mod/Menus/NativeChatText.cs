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

    public static IEnumerable<string> Segments(string text, Func<string, float>? measure = null, float maxWidth = float.MaxValue)
    {
        // Square brackets are game chat formatting/emoji tokens, not model markup.
        string clean = CompanionDialogueInbox.PlainText(text).Replace('[', '［').Replace(']', '］');
        foreach (var line in clean.Replace("\r", "").Split('\n'))
        {
            string remaining = line.Trim();
            while (remaining.Length > 0)
            {
                int length = Math.Min(160, remaining.Length);
                if (length < remaining.Length && char.IsHighSurrogate(remaining[length - 1])) length--;
                while (length > 2 && measure != null && measure(remaining[..length]) > maxWidth)
                {
                    length--;
                    if (char.IsHighSurrogate(remaining[length - 1])) length--;
                }
                yield return remaining[..length];
                remaining = remaining[length..];
            }
        }
    }
}
