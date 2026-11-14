//! One preflight reading shared by the command runner, CLI and screens.
use crate::{Failure, Host, Sentence, image, recipe::Recipe, runtime, snapshot};
use gb_config::agent_environment::{self, Resources};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

pub struct Device {
    pub view: Value,
    pub resources: Resources,
    pub snapshot: Option<PathBuf>,
    pub refusal: Option<Sentence>,
    pub warning: Option<Sentence>,
    dns_problem: Option<Sentence>,
}

impl Device {
    pub fn rendered(mut self, say: impl Fn(Sentence) -> String) -> Value {
        let refusal = self.refusal.map(&say).unwrap_or_default();
        let warning = self.warning.map(&say).unwrap_or_default();
        self.view["problem"] = json!(refusal);
        if let Some(problem) = self.dns_problem {
            self.view["dns"]["problem"] = json!(say(problem));
        }
        if self.view["problem_setting"] == "runtime" {
            self.view["runtime"]["reason"] = json!(refusal);
        }
        self.view["warning"] = json!(warning);
        self.view["image"]["warning"] = json!(warning);
        if self.view["problem_setting"] == "image" {
            self.view["image"]["problem"] = json!(refusal);
        }
        self.view
    }
}

pub async fn read(host: &Host, workspace: &Path) -> Device {
    let home = host.data.join("msb");
    let health = runtime::ensure(&home).err().map(Failure::sentence);
    let templates = host.package.join("templates");
    let config = workspace.join(".guildbotics/config");
    let mut device = Device {
        view: json!({"platform":std::env::consts::OS,
            "runtime":{"available":health.is_none(),"reason":"","version":runtime::RUNTIME_VERSION,"home":home},
            "resources":null,"snapshot":{"state":"missing","name":"","path":"","detail":"","output":[]},
            "image":{"default":"","architecture":image::device_architecture(),"reference":"","digest":"","digests":{},"present":true,"held":"","problem":"","warning":"","load_command":""},
            "network":null,"dns":{"declared":"","nameservers":[],"problem":""},
            "tools":[],"problem":"","problem_setting":"","warning":"",
            "access":{"documents":[],"paths":[],"denied":[],"problem":""},"members":[]}),
        resources: Resources::default(),
        snapshot: None,
        refusal: health,
        warning: None,
        dns_problem: None,
    };
    if device.refusal.is_some() {
        device.view["problem_setting"] = json!("runtime");
    }
    let declaration = (|| {
        device.resources = agent_environment::resources(&config, &templates)
            .map_err(|e| Sentence::Text(e.to_string()))?;
        device.view["resources"] =
            json!({"memory_mib":device.resources.memory_mib,"cpus":device.resources.cpus});
        let recipe = Recipe::load(&templates).map_err(|e| Sentence::Text(e.to_string()))?;
        device.view["image"]["default"] = json!(recipe.image);
        let image =
            agent_environment::image(&config, &templates).map_err(image::declaration_problem)?;
        Ok::<_, Sentence>((recipe, image))
    })();
    let (recipe, declared) = match declaration {
        Ok(value) => value,
        Err(problem) => {
            if device.refusal.is_none() {
                device.refusal = Some(problem);
                device.view["problem_setting"] = json!("declaration");
            }
            return device;
        }
    };
    match crate::contract::network_reading(&config, &templates, false) {
        Ok((_, view)) => {
            device.view["network"] = view["network"].clone();
            device.view["dns"] = view["dns"].clone();
        }
        Err(error) => {
            device.view["resources"] = Value::Null;
            device.dns_problem = Some(error.clone());
            if device.refusal.is_none() {
                device.refusal = Some(error);
                device.view["problem_setting"] = json!("declaration");
            }
        }
    }
    let mut held = None;
    let custom = declared.is_some();
    if let Some(declared) = declared {
        device.view["image"]["reference"] = json!(declared.reference);
        device.view["image"]["digests"] = json!(declared.digests);
        device.view["image"]["digest"] = json!(
            declared
                .digests
                .get(image::device_architecture())
                .cloned()
                .unwrap_or_default()
        );
        let command = image::load_command(&host.home).unwrap_or_default();
        device.view["image"]["load_command"] = json!(command);
        if device.refusal.is_none() {
            match runtime::held_image(&home, &declared.reference, &recipe.image).await {
                Ok(value) => held = value,
                Err(error) => {
                    device.refusal = Some(error.sentence());
                    device.view["problem_setting"] = json!("image");
                }
            }
        }
        device.view["image"]["held"] = json!(held.clone().unwrap_or_default());
        let status = image::status(
            &declared,
            image::device_architecture(),
            held.clone(),
            &command,
        );
        device.view["image"]["present"] =
            json!(status.refusal.is_none() && status.warning.is_none());
        device.warning = status.warning;
        if device.refusal.is_none() && status.refusal.is_some() {
            device.refusal = status.refusal;
            device.view["problem_setting"] = json!("image");
        }
    }
    let name = recipe.name(if custom {
        Some(held.as_deref().unwrap_or(""))
    } else {
        None
    });
    match snapshot::status(workspace, &name) {
        Ok(snapshot) => {
            let (state, detail) = match &snapshot.state {
                snapshot::State::Ready => ("ready", ""),
                snapshot::State::Missing => ("missing", ""),
                snapshot::State::Stale => ("stale", ""),
                snapshot::State::Building => ("building", ""),
                snapshot::State::Failed(detail) => ("failed", detail.as_str()),
            };
            device.view["snapshot"] =
                json!({"state":state,"name":name,"path":snapshot.path,"detail":detail,"output":[]});
            match snapshot.require_ready() {
                Ok(path) => device.snapshot = Some(path),
                Err(refusal) if device.refusal.is_none() => {
                    device.refusal = Some(refusal);
                    device.view["problem_setting"] = json!(if state == "building" {
                        "building"
                    } else {
                        "snapshot"
                    });
                }
                Err(_) => {}
            }
        }
        Err(error) => {
            if device.refusal.is_none() {
                device.refusal = Some(Sentence::Text(error.to_string()));
                device.view["problem_setting"] = json!("snapshot");
            }
        }
    }
    let registered = match registered_workspaces(&host.data) {
        Ok(value) => value,
        Err(error) => {
            if device.refusal.is_none() {
                device.refusal = Some(Sentence::Text(error));
                device.view["problem_setting"] = json!("filesystem");
            }
            return device;
        }
    };
    match crate::contract::resolve(workspace, &host.home, &registered, false, false) {
        Ok(access) => {
            device.view["access"] = access.view;
        }
        Err(error) => {
            device.view["access"]["problem"] = json!(error);
            if device.refusal.is_none() {
                device.refusal = Some(Sentence::Text(error));
                device.view["problem_setting"] = json!("filesystem");
            }
        }
    }
    device
}

pub use crate::registered_workspaces;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dns_problem_is_rendered_even_when_runtime_health_takes_precedence() {
        let problem = Sentence::Key(
            "intelligences.agent_environment.declaration.no_device_resolver",
            vec![
                ("where", "/config/environment.yml".into()),
                ("host", "host".into()),
            ],
        );
        for runtime_failed in [false, true] {
            let device = Device {
                view: json!({"problem_setting": if runtime_failed { "runtime" } else { "declaration" }, "runtime":{}, "dns":{}, "image":{}}),
                resources: Resources::default(),
                snapshot: None,
                refusal: Some(if runtime_failed {
                    Sentence::Text("runtime failed".into())
                } else {
                    problem.clone()
                }),
                warning: None,
                dns_problem: Some(problem.clone()),
            };
            let view = device.rendered(|sentence| match sentence {
                Sentence::Key(key, params) => {
                    assert_eq!(
                        key,
                        "intelligences.agent_environment.declaration.no_device_resolver"
                    );
                    assert_eq!(
                        params,
                        [
                            ("where", "/config/environment.yml".into()),
                            ("host", "host".into())
                        ]
                    );
                    "translated DNS advice".into()
                }
                Sentence::Text(text) => text,
                _ => panic!("unexpected sentence"),
            });
            assert_eq!(view["dns"]["problem"], "translated DNS advice");
            assert_eq!(
                view["problem"],
                if runtime_failed {
                    "runtime failed"
                } else {
                    "translated DNS advice"
                }
            );
        }
    }
}
