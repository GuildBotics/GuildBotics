!include "LogicLib.nsh"
!include "WinMessages.nsh"

!define GUILDBOTICS_PATH_ENTRY "%USERPROFILE%\.guildbotics\bin"
!define GUILDBOTICS_REGISTRY_KEY "Software\GuildBotics"
!define GUILDBOTICS_PATH_MARKER "path_entry_added"

!macro GUILDBOTICS_DEFINE_PATH_FUNCTIONS PREFIX LABEL
Function ${PREFIX}GuildBoticsNormalizePath
  Exch $0
  ExpandEnvStrings $0 "$0"

  guildbotics_trim_trailing_slash_${LABEL}:
    StrLen $1 "$0"
    IntCmp $1 0 guildbotics_normalize_done_${LABEL}
    IntOp $1 $1 - 1
    StrCpy $2 "$0" 1 $1
    StrCmp $2 "\" 0 guildbotics_normalize_done_${LABEL}
    StrCpy $0 "$0" $1
    Goto guildbotics_trim_trailing_slash_${LABEL}

  guildbotics_normalize_done_${LABEL}:
    Exch $0
FunctionEnd

Function ${PREFIX}GuildBoticsUserPathContainsEntry
  ReadRegStr $0 HKCU "Environment" "Path"
  StrCpy $1 "${GUILDBOTICS_PATH_ENTRY}"
  Push $1
  Call ${PREFIX}GuildBoticsNormalizePath
  Pop $1
  StrCpy $2 0
  StrCpy $3 ""

  guildbotics_contains_next_${LABEL}:
    StrCpy $4 "$0" 1 $2
    StrCmp $4 "" guildbotics_contains_segment_${LABEL}
    IntOp $2 $2 + 1
    StrCmp $4 ";" guildbotics_contains_segment_${LABEL}
    StrCpy $3 "$3$4"
    Goto guildbotics_contains_next_${LABEL}

  guildbotics_contains_segment_${LABEL}:
    Push $3
    Call ${PREFIX}GuildBoticsNormalizePath
    Pop $3
    StrCmp $3 $1 guildbotics_contains_found_${LABEL}
    StrCmp $4 "" guildbotics_contains_missing_${LABEL}
    StrCpy $3 ""
    Goto guildbotics_contains_next_${LABEL}

  guildbotics_contains_found_${LABEL}:
    Push 1
    Return
  guildbotics_contains_missing_${LABEL}:
    Push 0
FunctionEnd

Function ${PREFIX}GuildBoticsRemoveOwnedPathEntry
  ReadRegStr $0 HKCU "Environment" "Path"
  StrCpy $1 "${GUILDBOTICS_PATH_ENTRY}"
  Push $1
  Call ${PREFIX}GuildBoticsNormalizePath
  Pop $1
  StrCpy $2 0
  StrCpy $3 ""
  StrCpy $4 ""
  StrCpy $8 0

  guildbotics_remove_next_${LABEL}:
    StrCpy $5 "$0" 1 $2
    StrCmp $5 "" guildbotics_remove_segment_${LABEL}
    IntOp $2 $2 + 1
    StrCmp $5 ";" guildbotics_remove_segment_${LABEL}
    StrCpy $3 "$3$5"
    Goto guildbotics_remove_next_${LABEL}

  guildbotics_remove_segment_${LABEL}:
    StrCpy $6 "$3"
    Push $6
    Call ${PREFIX}GuildBoticsNormalizePath
    Pop $6
    StrCmp $6 $1 guildbotics_remove_owned_${LABEL}
    StrCmp $3 "" guildbotics_remove_continue_${LABEL}
    ${If} $4 == ""
      StrCpy $4 "$3"
    ${Else}
      StrCpy $4 "$4;$3"
    ${EndIf}
    Goto guildbotics_remove_continue_${LABEL}

  guildbotics_remove_owned_${LABEL}:
    StrCpy $8 1

  guildbotics_remove_continue_${LABEL}:
    StrCmp $5 "" guildbotics_remove_done_${LABEL}
    StrCpy $3 ""
    Goto guildbotics_remove_next_${LABEL}

  guildbotics_remove_done_${LABEL}:
    StrCmp $8 1 0 +3
    WriteRegExpandStr HKCU "Environment" "Path" "$4"
    SendMessage ${HWND_BROADCAST} ${WM_SETTINGCHANGE} 0 "STR:Environment" /TIMEOUT=5000
FunctionEnd
!macroend

!insertmacro GUILDBOTICS_DEFINE_PATH_FUNCTIONS "" "install"
!insertmacro GUILDBOTICS_DEFINE_PATH_FUNCTIONS "un." "uninstall"

!macro NSIS_HOOK_POSTINSTALL
  Call GuildBoticsUserPathContainsEntry
  Pop $0
  ${If} $0 == 0
    ReadRegStr $1 HKCU "Environment" "Path"
    ${If} $1 == ""
      StrCpy $1 "${GUILDBOTICS_PATH_ENTRY}"
    ${Else}
      StrCpy $1 "$1;${GUILDBOTICS_PATH_ENTRY}"
    ${EndIf}
    WriteRegExpandStr HKCU "Environment" "Path" "$1"
    WriteRegDWORD HKCU "${GUILDBOTICS_REGISTRY_KEY}" "${GUILDBOTICS_PATH_MARKER}" 1
    SendMessage ${HWND_BROADCAST} ${WM_SETTINGCHANGE} 0 "STR:Environment" /TIMEOUT=5000
  ${EndIf}
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  StrCpy $0 0
  ReadRegDWORD $0 HKCU "${GUILDBOTICS_REGISTRY_KEY}" "${GUILDBOTICS_PATH_MARKER}"
  ${If} $0 == 1
    Call un.GuildBoticsRemoveOwnedPathEntry
    DeleteRegValue HKCU "${GUILDBOTICS_REGISTRY_KEY}" "${GUILDBOTICS_PATH_MARKER}"
    DeleteRegKey /ifempty HKCU "${GUILDBOTICS_REGISTRY_KEY}"
  ${EndIf}
!macroend
