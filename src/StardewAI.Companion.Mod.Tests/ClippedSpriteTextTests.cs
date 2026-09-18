using Microsoft.Xna.Framework;
using Mono.Cecil;
using StardewAI.Companion.Mod.Menus;
using Xunit;

public class ClippedSpriteTextTests
{
    [Theory]
    [InlineData(12, 12, 12, 12, 30, 40, 8, 8)]
    [InlineData(6, 7, 10, 10, 34, 43, 4, 5)]
    [InlineData(16, 17, 16, 17, 30, 40, 4, 3)]
    public void ClipPreservesMatchingTexturePixels(int x, int y, int dx, int dy, int sx, int sy, int w, int h)
    {
        var position = new Vector2(x, y);
        var source = new Rectangle(30, 40, 8, 8);
        Assert.True(ClippedSpriteText.Clip(ref position, ref source, new Rectangle(10, 10, 10, 10)));
        Assert.Equal(new Vector2(dx, dy), position);
        Assert.Equal(new Rectangle(sx, sy, w, h), source);
    }

    [Fact]
    public void InvisibleGlyphDoesNotQueueDrawing()
    {
        var position = new Vector2(30, 30);
        var source = new Rectangle(0, 0, 8, 8);
        Assert.False(ClippedSpriteText.Clip(ref position, ref source, new Rectangle(10, 10, 10, 10)));
    }

    [Fact]
    public void CompiledProductionChatCannotResetSharedRenderingState()
    {
        using var assembly = AssemblyDefinition.ReadAssembly(typeof(ClippedSpriteText).Assembly.Location);
        var methods = assembly.MainModule.Types
            .Where(t => t.Name is "CompanionCommandMenu" or "ClippedSpriteText")
            .SelectMany(t => t.Methods).Where(m => m.HasBody);
        foreach (var method in methods)
        foreach (var instruction in method.Body.Instructions)
        {
            if (instruction.Operand is not MethodReference call) continue;
            Assert.False(call.DeclaringType.Name == "SpriteBatch" && call.Name is "Begin" or "End", call.FullName);
            Assert.False(call.Name is "set_ScissorRectangle" or "SetRenderTarget" or "SetRenderTargets" or "CreateScale", call.FullName);
        }
    }
}
