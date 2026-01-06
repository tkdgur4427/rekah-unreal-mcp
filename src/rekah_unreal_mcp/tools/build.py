"""Build tools for Unreal Engine projects."""

import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP


def register_build_tools(mcp: FastMCP):
    """Register build-related MCP tools."""

    @mcp.tool()
    def build_unreal_project(
        project_path: str,
        target: str = "Editor",
        config: str = "Development",
    ) -> str:
        """Build an Unreal Engine project.

        Args:
            project_path: Path to .uproject file
            target: Build target (Editor, Game, Server)
            config: Build configuration (Debug, Development, Shipping)

        Returns:
            Build output or error message
        """
        project = Path(project_path)

        if not project.exists():
            return f"Error: Project file not found: {project_path}"

        if not project.suffix == ".uproject":
            return f"Error: Not a .uproject file: {project_path}"

        # Find Engine root (assuming standard UE structure)
        engine_root = project.parent
        while engine_root.parent != engine_root:
            if (engine_root / "Engine" / "Build").exists():
                break
            engine_root = engine_root.parent
        else:
            return "Error: Could not find Unreal Engine root directory"

        # Build command (Windows)
        build_script = engine_root / "Engine" / "Build" / "BatchFiles" / "Build.bat"
        if not build_script.exists():
            return f"Error: Build script not found: {build_script}"

        project_name = project.stem
        cmd = [
            str(build_script),
            f"{project_name}{target}",
            "Win64",
            config,
            f"-project={project}",
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=1800,  # 30 minutes timeout
            )
            if result.returncode == 0:
                return f"Build succeeded:\n{result.stdout}"
            else:
                return f"Build failed:\n{result.stderr}\n{result.stdout}"
        except subprocess.TimeoutExpired:
            return "Error: Build timed out after 30 minutes"
        except Exception as e:
            return f"Error: Build failed with exception: {e}"
