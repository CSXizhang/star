using System.Runtime.CompilerServices;
using StardewAI.Companion.Mod.Observation;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public sealed class NativeMapImageFileTests
{
    [Fact]
    public void FinalizesNativeUnownedStreamBeforePublishingItsBufferedTrailer()
    {
        string path = Path.Combine(Path.GetTempPath(), "native-map-file-" + Guid.NewGuid().ToString("N") + ".png");
        try
        {
            WriteNativeStyleUnownedStream(path);
            NativeMapImageFile.EnsureComplete(path);
            Assert.Equal(82920, new FileInfo(path).Length);
            Assert.Equal(new byte[] { 0, 0, 0, 0, 73, 69, 78, 68, 174, 66, 96, 130 }, File.ReadAllBytes(path)[^12..]);
        }
        finally { File.Delete(path); }
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private static void WriteNativeStyleUnownedStream(string path)
    {
        // Reproduce the actual FileStream ownership/buffering bug, not rendering.
        var stream = new FileStream(path, FileMode.OpenOrCreate);
        stream.Write(new byte[81920]);
        var tail = new byte[1000];
        new byte[] { 0, 0, 0, 0, 73, 69, 78, 68, 174, 66, 96, 130 }.CopyTo(tail, tail.Length - 12);
        stream.Write(tail);
    }
}
