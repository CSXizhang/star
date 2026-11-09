namespace StardewAI.Companion.Mod.Observation;

internal static class NativeMapImageFile
{
    private static readonly byte[] End = { 0, 0, 0, 0, 73, 69, 78, 68, 174, 66, 96, 130 };

    public static void EnsureComplete(string path)
    {
        if (HasTrailer(path)) return;
        // Stardew 1.6.15 passes an unowned FileStream to SKData.SaveTo.
        // Its final sub-buffer may remain unwritten until finalization. Only
        // recover that rare native defect; never edit or regenerate PNG pixels.
        GC.Collect();
        GC.WaitForPendingFinalizers();
        if (!HasTrailer(path)) throw new InvalidDataException("Native map screenshot has no complete PNG trailer.");
    }

    private static bool HasTrailer(string path)
    {
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
        if (stream.Length < 24) return false;
        stream.Seek(-End.Length, SeekOrigin.End);
        var tail = new byte[End.Length];
        return stream.Read(tail, 0, tail.Length) == tail.Length && tail.AsSpan().SequenceEqual(End);
    }
}
