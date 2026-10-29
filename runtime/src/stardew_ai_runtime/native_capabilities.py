"""Versioned local native adapters. Build results are never gameplay verification.

The caller supplies discovery from the native host and binds player provenance. No
model-supplied approval flag or verification claim is accepted by this module.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import shutil
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape


class CapabilityError(ValueError):
    pass


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", value) or value in {".", ".."}:
        raise CapabilityError("Invalid capability identifier or version")
    return value


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def dotnet6_sdk_version(executable: str | Path | None) -> str | None:
    """Require the complete offline .NET 6 compiler, runtime and reference pack.

    A newer SDK alone cannot restore net6 references with our offline NuGet
    configuration. Pinning the installed 6 SDK also avoids ancestor global.json.
    """
    if not executable or not Path(executable).is_file():
        return None
    root = Path(executable).resolve().parent
    sdk_versions = [path.parent.name for path in (root / "sdk").glob("6.*/MSBuild.dll")
                    if re.fullmatch(r"6\.\d+\.\d+", path.parent.name)]
    if not sdk_versions:
        return None
    if not any((root / "packs/Microsoft.NETCore.App.Ref").glob("6.*/ref/net6.0/System.Text.Json.dll")):
        return None
    if not any((root / "shared/Microsoft.NETCore.App").glob("6.*/System.Private.CoreLib.dll")):
        return None
    return max(sdk_versions, key=lambda version: tuple(map(int, version.split("."))))


def discover_dotnet(search_root: Path | None = None) -> str | None:
    """Find a complete local .NET 6 SDK; runtime-only and newer SDKs do not qualify."""
    executable = "dotnet.exe" if os.name == "nt" else "dotnet"
    candidates = [shutil.which("dotnet")]
    if os.environ.get("DOTNET_ROOT"):
        candidates.append(str(Path(os.environ["DOTNET_ROOT"]) / executable))
    if search_root:
        for root in (search_root, *search_root.parents):
            candidates.extend(str(root / suffix / executable) for suffix in (".local/dotnet", "runtime/dotnet", "dotnet"))
    candidates.extend([str(Path.home() / ".dotnet" / executable), str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "dotnet" / executable)])
    return next((str(Path(p).resolve()) for p in candidates if dotnet6_sdk_version(p)), None)


class NativeCapabilityStore:
    def __init__(self, root: Path, *, dotnet: str | None = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        local_dotnet = self.root / "toolchain" / ("dotnet.exe" if os.name == "nt" else "dotnet")
        self.dotnet = (str(Path(dotnet).resolve()) if dotnet6_sdk_version(dotnet) else None) if dotnet else (str(local_dotnet) if dotnet6_sdk_version(local_dotnet) else discover_dotnet(self.root))
        self._builds: set[tuple[str, str]] = set()

    def package_path(self, capability: str, version: str) -> Path:
        return self.root / "packages" / _identifier(capability) / _identifier(version)

    def catalog(self) -> dict:
        return _read_json(self.root / "catalog.json", {"capabilities": {}})

    def inspect(self, capability: str, version: str | None = None) -> dict:
        if version is None:
            entry = self.catalog()["capabilities"].get(capability)
            if not entry:
                raise CapabilityError("Capability has no verified active version")
            version = entry["version"]
        path = self.package_path(capability, version)
        manifest = _read_json(path / "manifest.json")
        if not manifest:
            raise CapabilityError("Capability package does not exist")
        return {"manifest": manifest, "verification": _read_json(path / "verification.json"),
                "guidance": (path / "skill.md").read_text(encoding="utf-8"),
                "provenance": _read_json(path / "provenance.json", {})}

    def verify_artifact(self, capability: str, version: str) -> dict:
        package = self.package_path(capability, version)
        manifest = self.inspect(capability, version)["manifest"]
        if manifest.get("id") != capability or manifest.get("version") != version:
            raise CapabilityError("Package identity mismatch")
        assembly = manifest.get("assembly")
        if assembly != "adapter.dll":
            raise CapabilityError("Invalid assembly path")
        artifact = package / assembly
        if not artifact.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest() != manifest.get("sha256"):
            raise CapabilityError("Artifact hash mismatch or missing assembly")
        return manifest

    async def build(self, spec: dict, authoring: dict, *, provenance: dict | None = None) -> dict:
        """Compile supplied C# source in a separate process; keep all old versions."""
        capability, version = _identifier(spec["id"]), _identifier(spec["version"])
        key = (capability, version)
        destination = self.package_path(*key)
        if key in self._builds or destination.exists():
            raise CapabilityError("Version already exists or is building; use a new version")
        source = spec.get("source")
        if not isinstance(source, str) or not source.strip():
            raise CapabilityError("C# source is required")
        if spec.get("kind") not in {"action", "query"}:
            raise CapabilityError("Capability kind must be action or query")
        if not isinstance(spec.get("inputSchema"), dict) or spec["inputSchema"].get("type") != "object":
            raise CapabilityError("Input schema must describe an object")
        if not isinstance(spec.get("entryType"), str) or not re.fullmatch(r"[A-Za-z_][\w.]*", spec["entryType"]):
            raise CapabilityError("A qualified C# entryType is required")
        sdk_version = dotnet6_sdk_version(self.dotnet)
        if not sdk_version:
            return {"status": "setup_required", "message": "Install the .NET 6 SDK once, then retry this build. Existing capabilities remain available."}
        if authoring.get("targetFramework") != "net6.0":
            raise CapabilityError("Native host must provide its supported net6.0 authoring contract")
        references = authoring.get("references", [])
        if not references or any(not isinstance(p, str) or not Path(p).is_file() or Path(p).suffix.lower() != ".dll" for p in references):
            raise CapabilityError("Native host reference assemblies are missing")
        self._builds.add(key)
        attempt = self.root / "learning" / uuid.uuid4().hex
        attempt.mkdir(parents=True)
        record = {"id": capability, "version": version, "kind": spec["kind"],
                  "dependencies": spec.get("dependencies", []), "createdAtNs": time.time_ns(),
                  "status": "building", "provenance": provenance or {}}
        _write_json(attempt / "build.json", record)
        process = None
        try:
            _write_json(attempt / "global.json", {"sdk": {"version": sdk_version, "rollForward": "disable"}})
            (attempt / "Adapter.cs").write_text(source, encoding="utf-8")
            refs = "".join(f'<Reference Include="ref{i}"><HintPath>{escape(str(Path(p).resolve()))}</HintPath><Private>false</Private></Reference>' for i, p in enumerate(references))
            project = '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net6.0</TargetFramework><AssemblyName>adapter</AssemblyName><EnableDefaultCompileItems>false</EnableDefaultCompileItems><ImplicitUsings>enable</ImplicitUsings><Nullable>enable</Nullable><RestoreIgnoreFailedSources>true</RestoreIgnoreFailedSources></PropertyGroup><ItemGroup><Compile Include="Adapter.cs"/>' + refs + '</ItemGroup></Project>'
            (attempt / "Adapter.csproj").write_text(project, encoding="utf-8")
            (attempt / "NuGet.Config").write_text('<configuration><packageSources><clear/></packageSources></configuration>', encoding="utf-8")
            env = dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1", DOTNET_SKIP_FIRST_TIME_EXPERIENCE="1", MSBUILDDISABLENODEREUSE="1")
            process = await asyncio.create_subprocess_exec(self.dotnet, "build", "Adapter.csproj", "--configuration", "Release", "--output", "out", "--nologo", "-nodeReuse:false", "-p:ImportDirectoryBuildProps=false", "-p:ImportDirectoryBuildTargets=false", "-p:UseSharedCompilation=false", cwd=attempt, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            output, _ = await process.communicate()
            diagnostics = output.decode("utf-8", errors="replace")
            (attempt / "compiler.log").write_text(diagnostics, encoding="utf-8")
            artifact = attempt / "out/adapter.dll"
            if process.returncode != 0 or not artifact.is_file():
                record.update(status="build_failed", diagnostics=diagnostics[-16000:])
                _write_json(attempt / "build.json", record)
                return record
            manifest = {k: spec[k] for k in ("id", "version", "kind", "entryType", "inputSchema")}
            manifest.update(assembly="adapter.dll", sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(), contractVersion=1,
                            title=spec.get("title", capability), description=spec.get("description", ""), dependencies=spec.get("dependencies", []), guidance="skill.md")
            staging = attempt / "package"
            staging.mkdir()
            shutil.copy2(artifact, staging / "adapter.dll")
            (staging / "Adapter.cs").write_text(source, encoding="utf-8")
            (staging / "skill.md").write_text(spec.get("guidance", ""), encoding="utf-8")
            _write_json(staging / "manifest.json", manifest)
            _write_json(staging / "verification.json", {"status": "pending", "scope": "compile_only"})
            _write_json(staging / "provenance.json", provenance or {})
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(destination)
            record.update(status="built_pending_validation", manifest=manifest)
            _write_json(attempt / "build.json", record)
            return record
        except asyncio.CancelledError:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            record["status"] = "cancelled"
            _write_json(attempt / "build.json", record)
            raise
        except OSError as exc:
            record.update(status="build_failed", diagnostics=str(exc))
            _write_json(attempt / "build.json", record)
            return record
        finally:
            self._builds.discard(key)

    def record_native_validation(self, capability: str, version: str, receipt: dict) -> dict:
        """Caller passes a native response, NEVER model text or tool arguments."""
        manifest = self.verify_artifact(capability, version)
        if (receipt.get("id"), receipt.get("version"), receipt.get("sha256")) != (capability, version, manifest["sha256"]):
            raise CapabilityError("Native validation does not match the built artifact")
        if receipt.get("succeeded") is not True or not isinstance(receipt.get("evidence"), dict) or not receipt["evidence"]:
            raise CapabilityError("Successful native invocation with observed evidence is required")
        verification = {"status": "used_successfully", "scope": "native_invocation_only", "receipt": receipt}
        _write_json(self.package_path(capability, version) / "verification.json", verification)
        return verification

    def record_native_load(self, capability: str, version: str, receipt: dict) -> dict:
        """Record a host load only as trial readiness, without moving the catalog."""
        manifest = self.verify_artifact(capability, version)
        if (receipt.get("id"), receipt.get("version"), receipt.get("sha256")) != (capability, version, manifest["sha256"]) or receipt.get("loaded") is not True:
            raise CapabilityError("Native load receipt does not match the built artifact")
        verification = self.inspect(capability, version)["verification"] or {}
        if verification.get("status") == "used_successfully":
            verification["loadReceipt"] = receipt
        else:
            verification = {"status": "loaded_for_trial", "scope": "load_only", "receipt": receipt}
        _write_json(self.package_path(capability, version) / "verification.json", verification)
        return verification

    def rollback(self, capability: str) -> dict:
        """Move to a previously successful version after the caller reloads it."""
        catalog = self.catalog()
        current = catalog["capabilities"].get(capability, {})
        previous = current.get("previousVersion")
        if not previous:
            raise CapabilityError("No previous active version")
        manifest = self.verify_artifact(capability, previous)
        verification = self.inspect(capability, previous)["verification"]
        if not verification or verification.get("status") != "used_successfully":
            raise CapabilityError("Previous version has no successful invocation record")
        catalog["capabilities"][capability] = {"version": previous, "sha256": manifest["sha256"], "previousVersion": current["version"]}
        _write_json(self.root / "catalog.json", catalog)
        return catalog["capabilities"][capability]

    async def prepare_builder(self) -> dict:
        """Install Microsoft's hash-verified SDK locally after caller authorization.

        This only prepares the compiler. It neither executes adapters nor changes
        the user's global PATH. Caller records the real player download consent.
        """
        if dotnet6_sdk_version(self.dotnet):
            return {"status": "ready", "downloaded": False}
        self.dotnet = None
        if os.name != "nt" or platform.machine().lower() not in {"amd64", "x86_64", "arm64", "aarch64"}:
            return {"status": "setup_required", "message": "Install the .NET 6 SDK for this operating system."}
        rid = "win-arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "win-x64"
        destination = self.root / "toolchain"
        def install():
            metadata_url = "https://dotnetcli.blob.core.windows.net/dotnet/release-metadata/6.0/releases.json"
            with urllib.request.urlopen(metadata_url, timeout=60) as response:
                metadata = json.load(response)
            version = metadata["latest-sdk"]
            sdk = next(sdk for release in metadata["releases"] for sdk in release.get("sdks", [release.get("sdk", {})]) if sdk.get("version") == version)
            item = next(item for item in sdk["files"] if item["rid"] == rid and item["name"].endswith(".zip"))
            if not item["url"].startswith("https://"):
                raise CapabilityError("SDK metadata contains a non-HTTPS download")
            staging = self.root / ("toolchain-download-" + uuid.uuid4().hex)
            staging.mkdir()
            archive = staging / "sdk.zip"
            digest = hashlib.sha512()
            with urllib.request.urlopen(item["url"], timeout=60) as response, archive.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest().lower() != item["hash"].lower():
                raise CapabilityError("SDK download checksum mismatch; no compiler installed")
            unpack = staging / "sdk"
            unpack.mkdir()
            with zipfile.ZipFile(archive) as package:
                for member in package.infolist():
                    if not (unpack / member.filename).resolve().is_relative_to(unpack.resolve()):
                        raise CapabilityError("SDK archive contains an unsafe path")
                package.extractall(unpack)
            if not dotnet6_sdk_version(unpack / "dotnet.exe"):
                raise CapabilityError("SDK archive is incomplete")
            if destination.exists():
                raise CapabilityError("Local compiler directory already exists; inspect setup before retrying")
            unpack.rename(destination)
            archive.unlink()
            staging.rmdir()
            return version
        # The download worker cannot execute an adapter; cancellation leaves only
        # local setup files. Each compiler process has separate cancellation below.
        version = await asyncio.to_thread(install)
        self.dotnet = str(destination / "dotnet.exe")
        return {"status": "ready", "downloaded": True, "sdkVersion": version}

    def record_native_persist(self, capability: str, version: str, receipt: dict) -> dict:
        """Commit the catalog only after the host persisted the verified package."""
        manifest = self.verify_artifact(capability, version)
        verification = self.inspect(capability, version)["verification"]
        if not verification or verification.get("status") != "used_successfully":
            raise CapabilityError("Native invocation evidence is required before keeping a capability")
        if (receipt.get("id"), receipt.get("version"), receipt.get("sha256")) != (capability, version, manifest["sha256"]) or receipt.get("loaded") is not True or receipt.get("persisted") is not True:
            raise CapabilityError("A matching persisted native load receipt is required")
        catalog = self.catalog()
        previous = catalog["capabilities"].get(capability)
        if previous and previous.get("version") == version:
            return previous
        entry = {"version": version, "sha256": manifest["sha256"], "previousVersion": previous.get("version") if previous else None}
        catalog["capabilities"][capability] = entry
        _write_json(self.root / "catalog.json", catalog)
        return entry

    def list_packages(self) -> list[dict]:
        result = []
        for path in sorted((self.root / "packages").glob("*/*/manifest.json")):
            try:
                result.append(self.inspect(path.parent.parent.name, path.parent.name))
            except (CapabilityError, OSError, ValueError) as exc:
                result.append({"id": path.parent.parent.name, "version": path.parent.name, "error": str(exc)})
        return result

    def recent_learning_provenance(self, capability: str, *, save_id: str, kind: str,
                                   dependencies: list[dict] | None = None) -> dict | None:
        """Recover the latest real discussion for revisions of the same capability.

        Failed builds retain this discussion too. The caller still checks the live
        turn and feeds this source to learning_provenance; this grants no action.
        """
        _identifier(capability)
        expected_dependencies = dependencies or []
        candidates = []
        for path in (self.root / "learning").glob("*/build.json"):
            try:
                record = _read_json(path)
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            if record.get("id") != capability or record.get("kind") != kind or record.get("dependencies", []) != expected_dependencies:
                continue
            provenance = record.get("provenance")
            if not isinstance(provenance, dict):
                continue
            if (provenance.get("saveId") != save_id
                    or provenance.get("source") not in {"player_message", "player_decision"}
                    or not provenance.get("requestId") or not provenance.get("quote")):
                continue
            candidates.append((record.get("createdAtNs", 0), path.stat().st_mtime_ns, provenance))
        return dict(max(candidates, key=lambda item: item[:2])[2]) if candidates else None
