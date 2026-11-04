# Схемы Mermaid

Основной источник — два блока Mermaid в [README](../../README.md). Файлы `.mmd` содержат их точные копии. SVG экспортированы штатным Mermaid CLI 12.2.0, без ручного рисования элементов.

- [Архитектура SVG](architecture.svg) · [исходник Mermaid](architecture.mmd).
- [Последовательность SVG](sequence.svg) · [исходник Mermaid](sequence.mmd).

Для обновления экспорта после правки схемы перенесите содержимое соответствующего блока README в `.mmd`. Если установлен Node.js, из корня проекта выполните:

```text
npx --yes --package @mermaid-js/mermaid-cli@12.2.0 mmdc -i docs/diagrams/architecture.mmd -o docs/diagrams/architecture.svg --no-font-embed
```

`npx` запускает утилиту из npm-пакета. `--yes` разрешает загрузить указанный пакет, `--package` фиксирует его версию. `mmdc` — экспортёр Mermaid, `-i` выбирает входной файл, `-o` — выходной. `--no-font-embed` не вкладывает шрифты в SVG. Для второй схемы замените оба вхождения `architecture` на `sequence`. Для обычного использования практикума Node.js и эта команда не требуются.
