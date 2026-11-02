import { Button, Group, Modal, Stack, TextInput } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import * as api from "../api/endpoints";
import { useT } from "../i18n";
import { keys } from "../query/keys";
import { navigate } from "../router/Router";
import { href } from "../router/routes";
import { ErrorMessage } from "./common";

/** Creates a workspace and opens its first page. */
export function NewWorkspaceModal({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  const t = useT();
  const client = useQueryClient();
  const [name, setName] = useState("");
  const create = useMutation({
    mutationFn: () => api.workspaces.create(name.trim()),
    onSuccess: (w) => {
      setName("");
      onClose();
      navigate(href.workspace(w.name));
    },
    onSettled: () => void client.invalidateQueries({ queryKey: keys.workspaces() }),
  });
  return (
    <Modal opened={opened} onClose={onClose} title={t("workspace.new")} closeButtonProps={{ "aria-label": t("common.close") }} centered>
      <form
        aria-label={t("workspace.new")}
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <Stack gap="md">
          <TextInput
            label={t("workspace.name")}
            description={t("workspace.nameHelp")}
            placeholder="acme"
            value={name}
            onChange={(e) => setName(e.currentTarget.value)}
            required
            data-autofocus
          />
          {create.isError && <ErrorMessage error={create.error} />}
          <Group justify="flex-end">
            <Button variant="default" onClick={onClose}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={create.isPending || !name.trim()}>
              {t("workspace.create")}
            </Button>
          </Group>
        </Stack>
      </form>
    </Modal>
  );
}
