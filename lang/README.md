# External languages

Each translation is a JSON file named with a language code, for example:

- `ru.json`
- `en.json`
- `de.json`

The file must contain:

```json
{
  "meta": {
    "code": "de",
    "name": "Deutsch",
    "flag": "🇩🇪",
    "locale": "de-DE"
  },
  "strings": {
    "nav.overview": "Übersicht"
  }
}
```

The external host directory is mounted as `/lang` and overrides a built-in file with the same code. You can add or edit JSON files and then refresh the browser; rebuilding the Docker image is not required.
