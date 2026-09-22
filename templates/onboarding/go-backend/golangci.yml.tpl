run:
  timeout: 5m
  tests: true

linters:
  disable-all: true
  enable:
    - errcheck
    - govet
    - ineffassign
    - staticcheck
    - unused
    - gofmt
    - goimports
    - misspell
    - revive

linters-settings:
  misspell:
    locale: US
  errcheck:
    check-type-assertions: true
    check-blank: true
  revive:
    severity: warning

issues:
  exclude-use-default: false
  max-issues-per-linter: 0
  max-same-issues: 0
