# Claude Hooks

Публичный установщик хуков Claude Code для macOS, Linux и Windows.

[English version below](#english)

## Скачать

Текущая версия: **2.4.0**. Что нового — в [описании выпуска](https://github.com/bazuevv/Claude-Hooks/releases/tag/v2.4.0).

Скачайте установщик для своей системы из раздела [Releases](https://github.com/bazuevv/Claude-Hooks/releases/latest):

| Файл | Система |
|------|---------|
| `MacOS_Install-2.4.0.zip` | macOS 12 или новее, Intel и Apple Silicon |
| `Linux_Install-x86_64-2.4.0.AppImage` | Linux на Intel/AMD |
| `Linux_Install-aarch64-2.4.0.AppImage` | Linux на ARM |
| `Windows_Install-2.4.0.exe` | Windows 10 и 11 x64 |

Архивы «Source code» в выпуске содержат только этот README — установщиков в них нет.

## Первый запуск

### macOS

Распакуйте архив и откройте `MacOS_Install.app`.

Сборка не подписана сертификатом Apple Developer ID и не нотарифицирована Apple. Поэтому после первого двойного нажатия macOS может показать сообщение, в котором доступны только кнопки **Переместить в Корзину** и **Готово**.

Чтобы разрешить запуск:

1. Нажмите **Готово**.
2. Откройте **Системные настройки → Конфиденциальность и безопасность**.
3. Прокрутите страницу до раздела **Безопасность**.
4. Рядом с сообщением о `MacOS_Install.app` нажмите **Всё равно открыть**.
5. Подтвердите действие паролем или Touch ID, затем нажмите **Открыть**.

Кнопка доступна примерно в течение часа после неудачной попытки запуска. После подтверждения приложение открывается обычным двойным нажатием. [Официальная инструкция Apple](https://support.apple.com/ru-ru/guide/mac-help/-mh40616/mac).

### Linux

Выберите файл по архитектуре компьютера, разрешите его запуск и откройте двойным щелчком:

```bash
chmod +x Linux_Install-x86_64-2.4.0.AppImage
```

В файловом менеджере Nautilus то же самое: **Свойства → Разрешить запуск как программы**.

### Windows

Запустите `Windows_Install-2.4.0.exe` двойным щелчком. Файл не подписан, поэтому Windows может показать окно «Система Windows защитила ваш компьютер»: нажмите **Подробнее**, затем **Выполнить в любом случае**.

## Аккаунт API

`settings_API.json` входит в комплект как шаблон с `YOUR-API_KEY` и `YOUR-API-URL`. Укажите собственные данные подключения перед использованием API-аккаунта — или заведите аккаунт API в панели Accs кнопкой «+».

---

<a id="english"></a>

# Claude Hooks (English)

Public installer of Claude Code hooks for macOS, Linux and Windows.

## Download

Current version: **2.4.0**. See the [release notes](https://github.com/bazuevv/Claude-Hooks/releases/tag/v2.4.0) for what's new.

Download the installer for your system from [Releases](https://github.com/bazuevv/Claude-Hooks/releases/latest):

| File | System |
|------|--------|
| `MacOS_Install-2.4.0.zip` | macOS 12 or later, Intel and Apple Silicon |
| `Linux_Install-x86_64-2.4.0.AppImage` | Linux on Intel/AMD |
| `Linux_Install-aarch64-2.4.0.AppImage` | Linux on ARM |
| `Windows_Install-2.4.0.exe` | Windows 10 and 11 x64 |

The "Source code" archives of a release contain only this README, not the installers.

## First launch

### macOS

Unzip the archive and open `MacOS_Install.app`.

The build is not signed with an Apple Developer ID certificate and is not notarized by Apple. So after the first double-click macOS may show a message with only **Move to Trash** and **Done** buttons.

To allow it to run:

1. Click **Done**.
2. Open **System Settings → Privacy & Security**.
3. Scroll down to **Security**.
4. Next to the message about `MacOS_Install.app`, click **Open Anyway**.
5. Confirm with your password or Touch ID, then click **Open**.

The button is available for about an hour after the failed launch. After that the app opens with a normal double-click. [Apple's official guide](https://support.apple.com/guide/mac-help/mh40616/mac).

### Linux

Pick the file for your computer's architecture, make it executable and open it with a double-click:

```bash
chmod +x Linux_Install-x86_64-2.4.0.AppImage
```

In the Nautilus file manager: **Properties → Allow executing file as program**.

### Windows

Run `Windows_Install-2.4.0.exe` with a double-click. The file is not signed, so Windows may show "Windows protected your PC": click **More info**, then **Run anyway**.

## API account

`settings_API.json` is included as a template with `YOUR-API_KEY` and `YOUR-API-URL`. Fill in your own connection details before using the API account — or add an API account in the Accs panel with the "+" button.
