//! Opt-in product-path tests using the snapshot built in a disposable workspace.

use super::*;
use std::fs;

struct Device {
    dir: tempfile::TempDir,
    workspace: PathBuf,
    verification: PathBuf,
}

impl Device {
    fn new() -> Self {
        assert!(
            std::env::var_os("CI").is_none(),
            "real_device tests require an attending owner outside CI"
        );
        let verification = PathBuf::from(
            std::env::var_os("GUILDBOTICS_REAL_DEVICE_ROOT")
                .expect("GUILDBOTICS_REAL_DEVICE_ROOT must name the disposable verification root"),
        );
        let verification = fs::canonicalize(verification).unwrap();
        assert!(
            verification.join("guest-home").is_dir(),
            "missing disposable guest HOME"
        );
        let snapshots = gb_environment::snapshot::snapshots_dir(&verification.join("workspace"));
        let package = gb_base::home::python_package();
        let recipe = gb_environment::recipe::Recipe::load(&package.join("templates")).unwrap();
        gb_environment::snapshot::ready(&verification.join("workspace"), &recipe.name(None))
            .expect("build the snapshot from this checkout first");
        // Keep the runtime's Unix socket paths short, including on macOS.
        let dir = tempfile::Builder::new()
            .prefix("gb")
            .rand_bytes(6)
            .tempdir_in("/tmp")
            .unwrap();
        let workspace = workspace_with_a_member(dir.path());
        let destination = gb_environment::snapshot::snapshots_dir(&workspace);
        fs::create_dir_all(destination.parent().unwrap()).unwrap();
        std::os::unix::fs::symlink(fs::canonicalize(snapshots).unwrap(), destination).unwrap();
        Self {
            dir,
            workspace,
            verification,
        }
    }

    fn write(&self, name: &str, source: &str) {
        fs::write(
            self.workspace
                .join(".guildbotics/config/commands")
                .join(name),
            source,
        )
        .unwrap();
    }

    async fn run(&self, mut request: Value) -> (StatusCode, Value, Vec<String>) {
        let port = free_port();
        let mut command = server_command(self.dir.path(), port);
        command
            .env("GUILDBOTICS_WORKSPACE_ROOT", &self.workspace)
            .env("HOME", self.verification.join("guest-home"))
            .env_remove("GUILDBOTICS_IDENTITY_ROOT")
            .env("GUILDBOTICS_TEST_HOST_ONLY", "host")
            .env("GUILDBOTICS_LOG", "gb_environment=info,gb_server=info");
        let server = Server::spawn(command, self.dir.path(), port);
        request["person"] = json!("yuki");
        let result =
            tokio::time::timeout(Duration::from_secs(180), server.run(request, "en")).await;
        let logs = server.stderr.try_iter().collect();
        server.stop();
        let (status, reply) = result.expect("real_device command did not finish in 180 seconds");
        (status, reply, logs)
    }

    async fn python(&self, source: &str, cwd: &Path, message: &str) -> (String, Vec<String>) {
        self.write("probe.py", source);
        let (status, reply, logs) = self
            .run(json!({"command": "probe", "cwd": cwd, "message": message}))
            .await;
        assert_eq!(status, StatusCode::OK, "{reply} {logs:?}");
        (reply["output"].as_str().unwrap().trim().to_owned(), logs)
    }
}

#[tokio::test]
#[ignore = "real_device: owner approval and GUILDBOTICS_REAL_DEVICE_ROOT required"]
async fn real_device_snapshot_loads_the_read_only_guest_package() {
    let device = Device::new();
    let (output, _) = device
        .python(
            r#"
import os
from pathlib import Path
import site
import sys
import guildbotics
import guildbotics.guest.entry

COMMAND_METADATA = {"read_only": True}

def main():
    mount = Path("/opt/guildbotics/code/guildbotics")
    assert sys.version_info[:2] == (3, 15)
    assert Path(guildbotics.__file__) == mount / "__init__.py"
    assert (Path(site.getsitepackages()[0]) / "guildbotics.pth").read_text().strip() == str(mount.parent)
    assert "PYTHONPATH" not in os.environ
    try:
        (mount / "real-device-probe").touch()
    except OSError:
        return "read-only guest package"
    raise AssertionError("the product's code mount is writable")
"#,
            &device.workspace,
            "",
        )
        .await;
    assert_eq!(output, "read-only guest package");
}

#[tokio::test]
#[ignore = "real_device: owner approval and GUILDBOTICS_REAL_DEVICE_ROOT required"]
async fn real_device_checkout_and_code_mount_are_distinct() {
    let device = Device::new();
    let checkout = device.workspace.join("checkout");
    fs::create_dir_all(checkout.join("guildbotics")).unwrap();
    fixture_git(&checkout, &["init", "-q", "--template="]);
    fs::write(checkout.join("guildbotics/original"), "before").unwrap();
    fixture_git(&checkout, &["add", "guildbotics/original"]);
    let (output, _) = device
        .python(
            r#"
from pathlib import Path
import guildbotics

def main():
    assert Path(guildbotics.__file__).parent == Path("/opt/guildbotics/code/guildbotics")
    target = Path("guildbotics/original")
    assert target.read_text() == "before"
    target.write_text("changed in the VM")
    return "writable checkout"
"#,
            &checkout,
            "",
        )
        .await;
    assert_eq!(output, "writable checkout");
    assert_eq!(
        fs::read_to_string(checkout.join("guildbotics/original")).unwrap(),
        "changed in the VM"
    );
}

#[tokio::test]
#[ignore = "real_device: owner approval and GUILDBOTICS_REAL_DEVICE_ROOT required"]
async fn real_device_to_pdf_draws_japanese() {
    let device = Device::new();
    let (output, _) = device
        .python(
            r#"
from pathlib import Path
from subprocess import check_output
from types import SimpleNamespace

from guildbotics.commands.models import CommandSpec
from guildbotics.commands.to_pdf_command import ToPdfCommand

COMMAND_METADATA = {"read_only": True}

async def main():
    assert check_output(["fc-list", ":lang=ja", "family"]).strip()
    work = Path.cwd()
    spec = CommandSpec(
        name="inline_to_pdf", base_dir=work, command_class=ToPdfCommand, path=None,
        params={}, args=[], stdin_override=None, cwd=work, config={},
    )
    context = SimpleNamespace(pipe='# 見出し\n\n日本語の本文 and Latin text.', shared_state={})
    outcome = await ToPdfCommand(context, spec, work).run()
    return outcome.result[:5].decode("ascii")
"#,
            &device.workspace,
            "",
        )
        .await;
    assert_eq!(output, "%PDF-");
}

#[tokio::test]
#[ignore = "real_device: owner approval and GUILDBOTICS_REAL_DEVICE_ROOT required"]
async fn real_device_artifact_extraction_cannot_write_through_a_guest_link() {
    let device = Device::new();
    let outside = host_name(&device.dir.path().join("outside"));
    fs::create_dir(&outside).unwrap();
    let (output, _) = device
        .python(
            r#"
import io
import json
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

COMMAND_METADATA = {"read_only": True}

def main(context):
    data = io.BytesIO()
    with ZipFile(data, "w") as archive:
        archive.writestr("report/error.md", b"details")
    def unpack(path):
        return subprocess.run(
            [sys.executable, "-B", "-m", "guildbotics.guest.artifact_archive", path],
            input=data.getvalue(), capture_output=True, timeout=30,
        )
    completed = unpack("artifact")
    assert completed.returncode == 0, completed.stderr
    result = Path.cwd() / "artifact/report/error.md"
    assert json.loads(completed.stdout)["files"] == [str(result)]
    assert result.read_bytes() == b"details"
    Path("linked").symlink_to(context.pipe, target_is_directory=True)
    assert unpack("linked/artifact").returncode in {0, 1}
    return "archive extracted inside the VM"
"#,
            &device.workspace,
            outside.to_str().unwrap(),
        )
        .await;
    assert_eq!(output, "archive extracted inside the VM");
    assert_eq!(fs::read_dir(outside).unwrap().count(), 0);
}

#[tokio::test]
#[ignore = "real_device: owner approval and GUILDBOTICS_REAL_DEVICE_ROOT required"]
async fn real_device_command_runs_with_only_the_mounted_context() {
    let device = Device::new();
    let cwd = device.workspace.join("work");
    fs::create_dir(&cwd).unwrap();
    let (output, logs) = device
        .python(
            r#"
import logging
import os
from pathlib import Path
import platform

COMMAND_METADATA = {"read_only": True}

def main(context):
    logging.getLogger("guildbotics").warning("real-device-guest-warning")
    config = Path(os.environ["GUILDBOTICS_CONFIG_DIR"])
    assert (config / "team/project.yml").is_file()
    try:
        (config / "probe").touch()
    except OSError:
        pass
    else:
        raise AssertionError("workspace config is writable")
    return "|".join([
        platform.system(), context.person.person_id, str(os.getenv("GUILDBOTICS_TEST_HOST_ONLY")),
        os.getcwd(), context.pipe,
    ])
"#,
            &cwd,
            "the input",
        )
        .await;
    assert_eq!(
        output,
        format!("Linux|yuki|None|{}|the input", cwd.display())
    );
    assert!(
        logs.iter()
            .any(|line| line.contains("real-device-guest-warning")),
        "{logs:?}"
    );
    assert!(!device.workspace.join(".guildbotics/config/probe").exists());
}

#[tokio::test]
#[ignore = "real_device: owner approval and GUILDBOTICS_REAL_DEVICE_ROOT required"]
async fn real_device_command_output_and_child_exit_are_independent() {
    let device = Device::new();
    let start = std::time::Instant::now();
    let (output, logs) = device
        .python(
            r#"
import subprocess

COMMAND_METADATA = {"read_only": True}

def main():
    print("real-device-guest-stdout", flush=True)
    subprocess.Popen(["sleep", "90"])
    return "done"
"#,
            &device.workspace,
            "",
        )
        .await;
    assert_eq!(output, "done");
    assert!(
        logs.iter()
            .any(|line| line.contains("real-device-guest-stdout")),
        "{logs:?}"
    );
    assert!(start.elapsed() < Duration::from_secs(60));
}

#[tokio::test]
#[ignore = "real_device: owner approval and GUILDBOTICS_REAL_DEVICE_ROOT required"]
async fn real_device_subcommand_outside_the_mounts_is_refused() {
    let device = Device::new();
    device.write(
        "outer.yml",
        "read_only: true\ncommands:\n  - script: pwd\n    cwd: /etc\n",
    );
    let (status, reply, _) = device
        .run(json!({"command": "outer", "cwd": device.workspace}))
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{reply}");
    assert_eq!(reply["code"], "command_error");
    assert!(
        reply["message"].as_str().unwrap().contains("/etc"),
        "{reply}"
    );
}

#[tokio::test]
#[ignore = "real_device: owner approval and GUILDBOTICS_REAL_DEVICE_ROOT required"]
async fn real_device_template_sees_only_the_guest_context() {
    let device = Device::new();
    device.write(
        "reach.md",
        "---\nread_only: true\nbrain: none\ntemplate_engine: jinja2\n---\n\
         {{ context.integration_factory.__class__.__module__ }}\n",
    );
    let (status, reply, _) = device
        .run(json!({"command": "reach", "cwd": device.workspace}))
        .await;
    assert_eq!(status, StatusCode::OK, "{reply}");
    assert_eq!(
        reply["output"].as_str().unwrap().trim(),
        "guildbotics.guest.window"
    );
}

fn fixture_git(cwd: &Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .current_dir(cwd)
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_AUTHOR_NAME", "Fixture")
        .env("GIT_AUTHOR_EMAIL", "fixture@example.invalid")
        .env("GIT_COMMITTER_NAME", "Fixture")
        .env("GIT_COMMITTER_EMAIL", "fixture@example.invalid")
        .args(args)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{args:?}: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap().trim().to_owned()
}

#[tokio::test]
#[ignore = "real_device: owner approval and GUILDBOTICS_REAL_DEVICE_ROOT required"]
async fn real_device_member_git_runs_the_clone_inside_the_command() {
    let device = Device::new();
    let seed = device.workspace.join("seed");
    fs::create_dir(&seed).unwrap();
    fixture_git(&seed, &["init", "-qb", "main", "--template="]);
    fs::write(seed.join("original"), "initial").unwrap();
    fixture_git(&seed, &["add", "original"]);
    fixture_git(&seed, &["commit", "-qm", "initial"]);
    let bare = device
        .workspace
        .join(".guildbotics/local/services/code/acme%2Fdemo/repository.git");
    fs::create_dir_all(bare.parent().unwrap()).unwrap();
    fixture_git(
        &seed,
        &[
            "clone",
            "-q",
            "--bare",
            "--template=",
            ".",
            bare.to_str().unwrap(),
        ],
    );
    fs::write(
        device
            .workspace
            .join(".guildbotics/config/team/project.yml"),
        "name: Run\nlanguage: en\nservices:\n  code_hosting_service: {name: local, owner: acme}\n",
    )
    .unwrap();
    let clones = device.workspace.join(".guildbotics/local/clones/yuki");
    fs::create_dir_all(&clones).unwrap();
    let (output, _) = device
        .python(
            r#"
import json
from pathlib import Path
import subprocess

from guildbotics.guest.host_client import command_window

async def main():
    client = command_window()
    assert client is not None
    async def git(*args, stdin=""):
        result = await client.acall(
            "member", arguments=["git", *args, "--person", "yuki", "--format", "json"],
            stdin=stdin,
        )
        assert result["exit_code"] == 0, result["stderr"]
        return json.loads(result["stdout"])

    prepared = await git("prepare", "--repo", "acme/demo", "--branch", "probe")
    clone = Path(prepared["repo_path"])
    assert (clone / "original").read_text() == "initial"
    hook = clone / ".git/hooks/pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        '#!/bin/sh\n'
        'test -d /opt/guildbotics/code/guildbotics || exit 1\n'
        'echo inside > "$(git rev-parse --git-dir)/real-device-hook"\n'
    )
    hook.chmod(0o755)
    (clone / "probe").write_text("isolated commit")
    subprocess.run(["git", "add", "probe"], cwd=clone, check=True, timeout=30)
    published = await git("publish", "--repo-path", str(clone), "--content-stdin", stdin="probe")
    assert published["pushed"] is True
    assert (clone / ".git/real-device-hook").read_text().strip() == "inside"
    assert subprocess.check_output(
        ["git", "rev-parse", "refs/remotes/origin/probe"], cwd=clone, text=True, timeout=30,
    ).strip() == published["commit_sha"]
    return published["commit_sha"]
"#,
            &seed,
            "",
        )
        .await;
    assert_eq!(
        fixture_git(&bare, &["rev-parse", "refs/heads/probe"]),
        output
    );
    assert_eq!(
        fixture_git(&bare, &["show", "-s", "--format=%ae", &output]),
        "yuki@guildbotics.local"
    );
    assert_eq!(
        fixture_git(&bare, &["show", "probe:probe"]),
        "isolated commit"
    );
}

impl Device {
    fn account(tool: &str) -> Self {
        assert_eq!(
            std::env::var("GUILDBOTICS_REAL_DEVICE_ACCOUNT").as_deref(),
            Ok("1"),
            "real_device account tests require separate approval on a verification OS account"
        );
        let device = Self::new();
        let relative = format!("data/agent_environment/{tool}/login.sealed");
        let login = device.verification.join("machine").join(&relative);
        let target = device.dir.path().join("home").join(relative);
        assert!(fs::symlink_metadata(&login).unwrap().file_type().is_file());
        fs::create_dir_all(target.parent().unwrap()).unwrap();
        fs::copy(login, target).unwrap();
        let config = device.workspace.join(".guildbotics/config/intelligences");
        fs::create_dir_all(&config).unwrap();
        fs::write(
            config.join("cli_agent_mapping.yml"),
            format!("default: cli_agents/{tool}/default.yml\n"),
        )
        .unwrap();
        device
    }

    async fn turns(
        &self,
        tool: &str,
        cwd: &Path,
        session: &str,
        options: Value,
        prompts: &[&str],
        read_only: bool,
    ) -> Vec<Value> {
        let code = include_str!("smoke_turn.py");
        let code = if read_only {
            format!("{code}\nCOMMAND_METADATA = {{\"read_only\": True}}\n")
        } else {
            code.to_owned()
        };
        let request = json!({
            "tool": tool, "session": session, "options": options, "prompts": prompts,
        });
        let (output, _) = self.python(&code, cwd, &request.to_string()).await;
        serde_json::from_str(&output).unwrap()
    }

    fn usage(&self, tool: &str) -> Value {
        let mut command = Command::new(BIN);
        command
            .args(["environment", "usage", tool, "--workspace"])
            .arg(&self.workspace)
            .env("HOME", self.verification.join("guest-home"))
            .env("GUILDBOTICS_HOME", self.dir.path().join("home"))
            .env_remove("GUILDBOTICS_IDENTITY_ROOT");
        let output = command.output().unwrap();
        assert!(output.status.success(), "{tool} usage probe failed");
        serde_json::from_slice(&output.stdout).unwrap()
    }
}

fn smoke_options(tool: &str) -> Value {
    match tool {
        "copilot" => json!({"model": "gpt-5-mini", "reasoning_effort": "low"}),
        "antigravity" => json!({"model": "gemini-3.6-flash-low"}),
        _ => json!({}),
    }
}

async fn resume(tool: &str) {
    let device = Device::account(tool);
    let cwd = device.workspace.join("work");
    fs::create_dir(&cwd).unwrap();
    let options = smoke_options(tool);
    let first = device
        .turns(
            tool,
            &cwd,
            "",
            options.clone(),
            &["Reply with exactly OK."],
            false,
        )
        .await;
    let first = &first[0];
    assert_eq!(first["output"], "OK");
    let session = first["session"]
        .as_str()
        .filter(|id| !id.is_empty())
        .unwrap();
    let mut prompts = vec!["Reply with exactly AGAIN."];
    if tool == "copilot" {
        assert_eq!(
            first["settings"],
            json!({
                "model": "gpt-5-mini", "reasoning_effort": "low", "allow_all": "on",
                "requested": {"model": "gpt-5-mini", "reasoning_effort": "low", "allow_all": "on"},
                "rejected": [],
            })
        );
        prompts.push("Reply with exactly THIRD.");
    }
    let resumed = device
        .turns(tool, &cwd, session, options.clone(), &prompts, false)
        .await;
    assert_eq!(resumed[0]["output"], "AGAIN");
    for turn in &resumed {
        assert_eq!(turn["session"], session);
        assert_eq!(turn["replayed_ok"], false);
        if tool != "antigravity" {
            assert_eq!(turn["rehydrated"], true);
        }
    }
    match tool {
        "antigravity" => {
            assert_eq!(first["settings"]["model"], options["model"]);
            assert_eq!(first["initialized"]["model"], options["model"]);
            assert!(first["usage"]["input_tokens"].as_u64().unwrap() > 0);
        }
        "copilot" => {
            assert_eq!(resumed[0]["settings"]["reasoning_effort"], "low");
            assert_eq!(resumed[1]["output"], "THIRD");
        }
        "grok" => {
            let model = first["model"]
                .as_str()
                .filter(|name| !name.is_empty())
                .unwrap();
            for tokens in ["input_tokens", "output_tokens"] {
                assert!(first["usage"][tokens].as_u64().unwrap() > 0);
            }
            assert_eq!(resumed[0]["model"], model);
            assert_eq!(resumed[0]["unhandled_turn_completed"], false);
            let selected = device
                .turns(
                    tool,
                    &cwd,
                    "",
                    json!({"model": model}),
                    &["Reply with exactly OK."],
                    false,
                )
                .await;
            assert_eq!(selected[0]["output"], "OK");
            assert_eq!(selected[0]["model"], model);
        }
        _ => unreachable!(),
    }
}

#[tokio::test]
#[ignore = "real_device: attended Copilot inference with a verification account"]
async fn real_device_copilot_resume() {
    resume("copilot").await;
}

#[tokio::test]
#[ignore = "real_device: attended Grok Build inference with a verification account"]
async fn real_device_grok_resume() {
    resume("grok").await;
}

#[tokio::test]
#[ignore = "real_device: attended Antigravity inference with a verification account"]
async fn real_device_antigravity_resume() {
    resume("antigravity").await;
}

#[tokio::test]
#[ignore = "real_device: attended Antigravity inference with a verification account"]
async fn real_device_antigravity_cwd() {
    let device = Device::account("antigravity");
    let cwd = device.workspace.join("work");
    fs::create_dir(&cwd).unwrap();
    fs::write(cwd.join("marker.txt"), "guildbotics-marker\n").unwrap();
    let results = device
        .turns(
            "antigravity",
            &cwd,
            "",
            smoke_options("antigravity"),
            &["Read marker.txt in the current directory and reply with its contents only."],
            false,
        )
        .await;
    assert_eq!(results[0]["output"], "guildbotics-marker");
}

async fn read_only_turn(tool: &str) {
    let device = Device::account(tool);
    let cwd = device.workspace.join("work");
    fs::create_dir(&cwd).unwrap();
    let grant = host_name(&device.dir.path().join("grant"));
    fs::create_dir(&grant).unwrap();
    fs::write(
        device
            .workspace
            .join(".guildbotics/local/cli_agent_filesystem_grants.yml"),
        json!({"paths": [{"path": grant, "access": "read_write"}]}).to_string(),
    )
    .unwrap();
    fs::write(
        device.workspace.join(".guildbotics/config/intelligences/agent_environment.yml"),
        "network: {mode: allowlist, allowed_domains: [example.com]}\ndns: {nameservers: [1.1.1.1]}\n",
    )
    .unwrap();
    let path = grant.join("smoke.txt");
    let mut prompt = format!(
        "Try exactly once to create {} with the word HELLO. Reply with WRITE=ok or WRITE=failed.",
        path.display()
    );
    if tool == "antigravity" {
        prompt.push_str(" Also run curl -sS --max-time 5 -o /dev/null -w '%{http_code}' https://example.com. Add HTTP=<status or failed> on another line.");
    }
    let results = device
        .turns(tool, &cwd, "", smoke_options(tool), &[&prompt], true)
        .await;
    let output = results[0]["output"].as_str().unwrap();
    assert!(output.lines().any(|line| line == "WRITE=failed"));
    assert!(!path.exists());
    if tool == "antigravity" {
        assert!(
            output
                .lines()
                .any(|line| matches!(line, "HTTP=failed" | "HTTP=000"))
        );
    } else {
        assert_eq!(results[0]["settings"]["allow_all"], "on");
    }
}

#[tokio::test]
#[ignore = "real_device: attended Copilot inference with a verification account"]
async fn real_device_copilot_read_only() {
    read_only_turn("copilot").await;
}

#[tokio::test]
#[ignore = "real_device: attended Antigravity inference with a verification account"]
async fn real_device_antigravity_read_only() {
    read_only_turn("antigravity").await;
}

#[test]
#[ignore = "real_device: attended Copilot quota request with a verification account"]
fn real_device_copilot_usage() {
    let snapshot = Device::account("copilot").usage("copilot");
    assert_eq!(snapshot["agent"], "copilot");
    let windows = snapshot["windows"].as_array().unwrap();
    assert!(!windows.is_empty());
    for window in windows {
        assert!((0.0..=100.0).contains(&window["used_percent"].as_f64().unwrap()));
        assert_eq!(window["label"], window["window"]);
    }
}

#[test]
#[ignore = "real_device: attended Antigravity usage request with a verification account"]
fn real_device_antigravity_usage() {
    let device = Device::account("antigravity");
    let snapshot = device.usage("antigravity");
    assert_eq!(snapshot["agent"], "antigravity");
    assert!(!snapshot["windows"].as_array().unwrap().is_empty());
    assert!(
        !device
            .dir
            .path()
            .join("home/data/agent_environment/antigravity/sessions")
            .exists()
    );
}
