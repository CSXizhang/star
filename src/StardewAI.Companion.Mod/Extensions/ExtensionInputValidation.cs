using System.Text.Json.Nodes;
namespace StardewAI.Companion.Mod.Extensions;
public static class ExtensionInputValidation
{
    public static void Validate(JsonObject schema, JsonNode? value, string path = "arguments")
    {
        string? type = schema["type"]?.GetValue<string>();
        bool valid = type switch {
            null => true, "object" => value is JsonObject, "array" => value is JsonArray,
            "string" => value is JsonValue v && v.TryGetValue<string>(out _),
            "boolean" => value is JsonValue b && b.TryGetValue<bool>(out _),
            "number" => double.TryParse(value?.ToJsonString(), System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out _),
            "integer" => long.TryParse(value?.ToJsonString(), out _), "null" => value == null,
            _ => throw new ArgumentException($"Unsupported schema type {type}.") };
        if (!valid) throw new ArgumentException($"{path} must be {type}.");
        if (schema["enum"] is JsonArray options && !options.Any(x => x?.ToJsonString() == value?.ToJsonString())) throw new ArgumentException($"{path} is outside enum.");
        if (value is JsonObject obj)
        {
            foreach (var key in (schema["required"] as JsonArray ?? new()).Select(x=>x!.GetValue<string>()))
                if (!obj.ContainsKey(key)) throw new ArgumentException($"{path}.{key} is required.");
            var props = schema["properties"] as JsonObject ?? new();
            foreach (var entry in obj)
                if (props[entry.Key] is JsonObject rule) Validate(rule,entry.Value,path+"."+entry.Key);
                else if (schema["additionalProperties"] is JsonValue extra && extra.TryGetValue<bool>(out bool allow) && !allow) throw new ArgumentException($"Unexpected {path}.{entry.Key}.");
        }
        if (value is JsonArray array)
        {
            if (schema["minItems"] is JsonValue min && array.Count < min.GetValue<int>() || schema["maxItems"] is JsonValue max && array.Count > max.GetValue<int>()) throw new ArgumentException($"{path} has invalid length.");
            if (schema["items"] is JsonObject item) for(int i=0;i<array.Count;i++) Validate(item,array[i],$"{path}[{i}]");
        }
        if (type is "number" or "integer" && double.TryParse(value!.ToJsonString(), System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture,out double number))
            if (schema["minimum"] is JsonValue min && number < min.GetValue<double>() || schema["maximum"] is JsonValue max && number > max.GetValue<double>()) throw new ArgumentException($"{path} is outside bounds.");
    }
}
