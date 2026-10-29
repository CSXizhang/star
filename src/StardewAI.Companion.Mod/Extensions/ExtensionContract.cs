using System.Text.Json.Nodes;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;
namespace StardewAI.Companion.Mod.Extensions;

/// <summary>Generated adapters execute only through a bound, main-thread invocation.</summary>
public interface IGameExtension { IExtensionExecution Begin(ExtensionContext context, JsonObject arguments); }
public interface IExtensionExecution : IDisposable
{
    ExtensionOutcome? Tick(GameTime time);
    void Cancel();
}
public sealed record ExtensionOutcome(bool Succeeded, JsonObject Evidence, string? Error = null);
public sealed record ExtensionContext(IFarmerActor Actor, IWorldObserver Observer, IModRegistry ModRegistry, CancellationToken CancellationToken);
public sealed record ExtensionDependency(string Id, string Version);
public sealed record ExtensionManifest(string Id, string Version, string Title, string Kind, string EntryType,
    string Assembly, JsonObject InputSchema, string Guidance, ExtensionDependency[] Dependencies, string Sha256, int ContractVersion = 1);
