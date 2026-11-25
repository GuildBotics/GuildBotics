import {
  Alert,
  Badge,
  Button,
  Card,
  Code,
  CopyButton,
  Group,
  Loader,
  Modal,
  Radio,
  Stack,
  Text,
  TextInput,
} from "@mantine/core";
import { Check, Copy, Plus, TriangleAlert } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { isApiError, type EnvironmentImageStatus } from "../api/client";
import * as api from "../repository/api/endpoints";
import type {
  CachedImage,
  CiSettings as Settings,
  ImageOperation,
  MissingImage,
} from "../repository/api/types";
import {
  ErrorMessage,
  Loading,
  Notice,
  Refusal,
  StateBadge,
  Time,
} from "../repository/components/common";
import { useT, type MessageKey } from "../repository/i18n";
import { useCiChange, useCiSettings } from "../repository/query/hooks";
import { useFragmentTarget } from "../repository/router/Router";
import { Header } from "../ui/Header";
import { EnvironmentCommand } from "./EnvironmentCommand";

/** An image's size as people read it (the cache holds images of hundreds of MiB to GiB). */
function size(bytes: number): string {
  const gib = bytes / 2 ** 30;
  return gib >= 1 ? `${gib.toFixed(1)} GiB` : `${Math.max(1, Math.round(bytes / 2 ** 20))} MiB`;
}

/** A digest cut in the middle: `sha256:` and its first 8 hex digits, then the last 9 characters. */
function shorten(digest: string): string {
  return digest.length > 24 ? `${digest.slice(0, 15)}…${digest.slice(-9)}` : digest;
}

export const IMAGE_SECTION_ID = "agent-environment-image";

export function EnvironmentImages({
  baseImage,
  focusElement,
  manageImages,
  workspace,
}: {
  baseImage?: EnvironmentImageStatus;
  focusElement?: string;
  manageImages: boolean;
  workspace: string | null;
}) {
  const repoT = useT();
  const settings = useCiSettings(manageImages);
  const { images, operation, executor } = (manageImages ? settings.data : undefined) ?? {};
  const [adding, setAdding] = useState(false);
  const fragment = useFragmentTarget(IMAGE_SECTION_ID);
  const baseReference = baseImage?.reference || baseImage?.default;
  const belongsHere = ({ repository }: { repository: string }) =>
    workspace === null || repository.startsWith(`${workspace}/`);
  const missing = (images?.missing ?? [])
    .map((image) => ({ ...image, repositories: image.repositories.filter(belongsHere) }))
    .filter((image) => image.repositories.length > 0);
  const baseCached = images?.cached.some((image) => image.reference === baseReference);
  const baseListed = baseCached || missing.some((image) => image.image === baseReference);
  const added = new Set(
    operation?.state === "succeeded" ? operation.loaded.map((image) => image.reference) : [],
  );

  return (
    <Card
      withBorder
      p="md"
      component="section"
      id={IMAGE_SECTION_ID}
      aria-label={repoT("ci.settings.images")}
      style={{
        scrollMarginTop: 24,
        outline:
          focusElement === IMAGE_SECTION_ID ? "1px solid var(--mantine-color-teal-6)" : undefined,
      }}
      {...fragment}
    >
      <Header
        order={4}
        title={repoT("ci.settings.images")}
        description={manageImages ? repoT("ci.settings.sharedImages") : undefined}
        action={
          manageImages &&
          settings.isSuccess && (
            <Button
              size="xs"
              variant="default"
              leftSection={<Plus size={14} />}
              onClick={() => setAdding(true)}
            >
              {repoT("ci.settings.addImage")}
            </Button>
          )
        }
      />
      <Stack gap="md">
        {baseImage && !baseListed && <BaseImage image={baseImage} showReference />}
        {manageImages && settings.isPending && <Loading />}
        {manageImages && settings.isError && <ErrorMessage error={settings.error} />}
        {executor && (
          <Text size="xs" c="dimmed">
            {repoT("ci.settings.imageCache")} <Code>{executor.home}</Code>
          </Text>
        )}
        {images?.error && <Refusal>{images.error}</Refusal>}
        {operation?.state === "running" && (
          <Text size="sm" c="dimmed" role="status">
            {repoT("ci.settings.adding", { target: operation.target })}
          </Text>
        )}
        {missing.map((image) => (
          <Missing
            key={image.image}
            image={image}
            baseImage={!baseCached && image.image === baseReference ? baseImage : undefined}
          />
        ))}
        {images && images.cached.length > 0 ? (
          <Stack gap="sm" role="list" aria-label={repoT("ci.settings.cached")}>
            {images.cached.map((image) => (
              <Cached
                key={image.reference}
                image={{ ...image, used_by: image.used_by.filter(belongsHere) }}
                added={added.has(image.reference)}
                baseImage={image.reference === baseReference ? baseImage : undefined}
              />
            ))}
          </Stack>
        ) : (
          images &&
          !images.error && (
            <Stack gap={4} align="center" ta="center" py="lg">
              <Text fw={600}>{repoT("ci.settings.emptyTitle")}</Text>
              <Text size="sm" c="dimmed">
                {repoT("ci.settings.emptyHelp")}
              </Text>
            </Stack>
          )
        )}
      </Stack>
      {manageImages && (
        <AddImage operation={operation ?? null} opened={adding} onClose={() => setAdding(false)} />
      )}
    </Card>
  );
}

/** An image pipelines name that the cache does not hold. */
function Missing({
  image: m,
  baseImage,
}: {
  image: MissingImage;
  baseImage?: EnvironmentImageStatus;
}) {
  const repoT = useT();
  const users = m.repositories
    .map((r) => repoT("ci.settings.jobsOf", { jobs: r.jobs.join(", "), repository: r.repository }))
    .join(" · ");
  return (
    <Alert
      color="warning"
      icon={<TriangleAlert size={18} />}
      title={repoT("ci.settings.missingTitle", { users })}
    >
      <Stack gap={4}>
        <code className="secret">{m.image}</code>
        <Text size="sm">{repoT("ci.settings.missingAbsent")}</Text>
        {baseImage && <BaseImage image={baseImage} showReference={false} />}
      </Stack>
    </Alert>
  );
}

function Cached({
  image: c,
  added,
  baseImage,
}: {
  image: CachedImage;
  added: boolean;
  baseImage?: EnvironmentImageStatus;
}) {
  const repoT = useT();
  const users = c.used_by.map((u) => `${u.repository} (${u.jobs.join(", ")})`).join(" · ");
  return (
    <div role="listitem" className="setting-row">
      <div className="setting-row-name">
        <Group gap="xs" wrap="nowrap">
          <Text size="sm" fw={600} title={c.reference} truncate style={{ minWidth: 0 }}>
            {c.reference}
          </Text>
          {added && <StateBadge tone="success">{repoT("ci.settings.added")}</StateBadge>}
        </Group>
        <Text size="xs" c="dimmed">
          {size(c.size_bytes)} · <Time value={c.created_at} date />
          {users && ` · ${repoT("ci.settings.usedBy", { repositories: users })}`}
        </Text>
        <Text size="xs" c="dimmed" className="mono" title={c.digest} truncate>
          {shorten(c.digest)}
        </Text>
        {baseImage && <BaseImage image={baseImage} showReference={false} />}
      </div>
      <CopyReference value={c.reference} />
    </div>
  );
}

/** Copies an image's tag: the value to write as `image` in `.guildbotics-ci.toml`. */
function CopyReference({ value }: { value: string }) {
  const repoT = useT();
  return (
    <CopyButton value={value} timeout={2000}>
      {({ copied, copy }) => (
        <Button
          size="compact-xs"
          variant="default"
          leftSection={copied ? <Check size={12} /> : <Copy size={12} />}
          onClick={copy}
        >
          {repoT(copied ? "common.copied" : "ci.settings.copyReference")}
        </Button>
      )}
    </CopyButton>
  );
}

type Source = "pull" | "load";

/** Where an image comes from: the one field the dialog asks for, and the operation that adds it. */
const SOURCES: Record<
  Source,
  {
    label: MessageKey;
    field: MessageKey;
    description?: MessageKey;
    placeholder: string;
    add: (value: string) => Promise<Settings>;
  }
> = {
  pull: {
    label: "ci.settings.pull",
    field: "ci.settings.pullReference",
    placeholder: "debian:bookworm",
    add: api.admin.pullImage,
  },
  load: {
    label: "ci.settings.load",
    field: "ci.settings.loadPath",
    description: "ci.settings.loadHelp",
    placeholder: "/path/to/image.tar",
    add: api.admin.loadImage,
  },
};

/** The "Add image" dialog: it starts a pull or a load and follows it (the page polls a running operation) to its outcome. */
function AddImage({
  operation,
  opened,
  onClose,
}: {
  operation: ImageOperation | null;
  opened: boolean;
  onClose: () => void;
}) {
  const repoT = useT();
  const [source, setSource] = useState<Source>("pull");
  const [value, setValue] = useState("");
  // Whether this opening of the dialog started an operation: its outcome stays shown until the dialog closes.
  const [started, setStarted] = useState(false);
  const add = useCiChange((s: { source: Source; value: string }) => SOURCES[s.source].add(s.value));
  const running = operation?.state === "running";
  const shown = operation && (running || started) ? operation : null;
  const { field, description, placeholder } = SOURCES[source];
  function close() {
    setSource("pull");
    setValue("");
    setStarted(false);
    add.reset();
    onClose();
  }
  return (
    <Modal
      opened={opened}
      onClose={close}
      title={repoT("ci.settings.addImage")}
      closeButtonProps={{ "aria-label": repoT("common.close") }}
      centered
    >
      <form
        aria-label={repoT("ci.settings.addImage")}
        onSubmit={(e) => {
          e.preventDefault();
          add.mutate(
            { source, value: value.trim() },
            {
              onSuccess: () => {
                setStarted(true);
                setValue("");
              },
            },
          );
        }}
      >
        <Stack gap="md">
          <Radio.Group
            label={repoT("ci.settings.source")}
            value={source}
            onChange={(v) => setSource(v as Source)}
          >
            <Stack gap="xs" mt={6}>
              {(["pull", "load"] as const).map((s) => (
                <Radio key={s} value={s} label={repoT(SOURCES[s].label)} />
              ))}
            </Stack>
          </Radio.Group>
          <TextInput
            label={repoT(field)}
            description={description && repoT(description)}
            placeholder={placeholder}
            value={value}
            onChange={(e) => setValue(e.currentTarget.value)}
            data-autofocus
          />
          {shown && <Outcome operation={shown} />}
          {add.isError &&
            (isApiError(add.error, "conflict") ? (
              <Refusal>{repoT("ci.settings.busy")}</Refusal>
            ) : (
              <ErrorMessage error={add.error} />
            ))}
          <Group justify="flex-end">
            <Button type="submit" disabled={running || add.isPending || !value.trim()}>
              {repoT("ci.settings.addSubmit")}
            </Button>
          </Group>
        </Stack>
      </form>
    </Modal>
  );
}

/** What the operation is doing, or what it brought or why it failed. */
function Outcome({ operation: op }: { operation: ImageOperation }) {
  const repoT = useT();
  if (op.state === "running") {
    return (
      <Group gap="xs" role="status" wrap="nowrap">
        <Loader size="xs" />
        <Text size="sm">{repoT("ci.settings.adding", { target: op.target })}</Text>
      </Group>
    );
  }
  if (op.state === "failed") return op.detail ? <Refusal>{op.detail}</Refusal> : null;
  return (
    <Stack gap={6} role="list" aria-label={repoT("ci.settings.loaded")}>
      {op.loaded.map((l) => (
        <div key={l.reference} role="listitem" className="setting-row">
          <span className="setting-row-name">
            <Notice>{repoT("ci.settings.addedImage", { reference: l.reference })}</Notice>
          </span>
          <CopyReference value={l.reference} />
        </div>
      ))}
    </Stack>
  );
}

/** The declared base image on this device, with the command that loads it. */
function BaseImage({
  image,
  showReference,
}: {
  image: EnvironmentImageStatus;
  showReference: boolean;
}) {
  const { t } = useTranslation();
  if (!image.reference) {
    return (
      <Group gap="xs">
        <Text size="sm" fw={600}>
          {t("setup.intelligence.environment.image")}
        </Text>
        <Badge color="gray" variant="light" size="sm">
          {t("setup.intelligence.environment.imageDefault")}
        </Badge>
        {showReference && (
          <Text size="xs" c="dimmed" ff="monospace">
            {image.default}
          </Text>
        )}
      </Group>
    );
  }
  // Nothing loaded is a problem; loaded but not what is declared is a
  // warning, the turns run on it; loaded and declared is fine.
  const state = !image.held
    ? "imageMissing"
    : image.present
      ? "imagePresent"
      : image.digest
        ? "imageDiffers"
        : "imageUndeclared";
  return (
    <Stack gap="xs" py={4}>
      <Group gap="xs">
        <Text size="sm" fw={600}>
          {t("setup.intelligence.environment.image")}
        </Text>
        <Badge
          color={
            state === "imagePresent" ? "success" : state === "imageMissing" ? "danger" : "warning"
          }
          variant="light"
          size="sm"
        >
          {t(`setup.intelligence.environment.${state}`)}
        </Badge>
        <Text size="xs" c="dimmed" ff="monospace">
          {showReference && `${image.reference} `}
          {image.architecture}
          {image.held ? ` ${image.held.slice(0, "sha256:".length + 12)}` : ""}
        </Text>
      </Group>
      <Text size="xs" c="dimmed">
        {t("setup.intelligence.environment.imageDeclaredFor", {
          architectures: Object.keys(image.digests).sort().join(", "),
        })}
      </Text>
      {image.problem ? <Text size="sm">{image.problem}</Text> : null}
      {image.warning ? <Text size="sm">{image.warning}</Text> : null}
      {image.present ? null : (
        <>
          <Text size="xs" c="dimmed">
            {t("setup.intelligence.environment.imageLoadHint")}
          </Text>
          <EnvironmentCommand command={image.load_command} />
        </>
      )}
    </Stack>
  );
}
