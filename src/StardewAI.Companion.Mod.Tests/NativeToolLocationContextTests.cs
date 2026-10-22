using System.Runtime.Serialization;
using HarmonyLib;
using StardewValley;
using StardewAI.Companion.Mod.Adapters;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

[CollectionDefinition("Native tool ambient location", DisableParallelization = true)]
public class NativeToolLocationCollection { }

[Collection("Native tool ambient location")]
public class NativeToolLocationContextTests
{
    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void NativeToolSeesCompanionMapAndRestoresPlayerMapEvenOnFailure(bool fail)
    {
        var gameField = AccessTools.Field(typeof(Game1), "game1");
        var locationField = AccessTools.Field(typeof(Game1), "instanceGameLocation");
        var oldGame = gameField.GetValue(null);
        var game = (Game1)FormatterServices.GetUninitializedObject(typeof(Game1));
        var town = (GameLocation)FormatterServices.GetUninitializedObject(typeof(GameLocation));
        var farm = (GameLocation)FormatterServices.GetUninitializedObject(typeof(GameLocation));
        try
        {
            gameField.SetValue(null, game);
            locationField.SetValue(game, town);
            void UseTool() => NativeToolLocationContext.Run(farm, () =>
            {
                Assert.Same(farm, Game1.currentLocation);
                if (fail) throw new InvalidOperationException("native tool failed");
            });
            if (fail) Assert.Throws<InvalidOperationException>(UseTool);
            else UseTool();
            Assert.Same(town, Game1.currentLocation);
        }
        finally
        {
            gameField.SetValue(null, oldGame);
        }
    }
}
