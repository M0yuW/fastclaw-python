package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"time"

	"github.com/fastclaw-ai/fastclaw/internal/provider"
	"github.com/fastclaw-ai/fastclaw/internal/session"
	"github.com/fastclaw-ai/fastclaw/internal/store"
	"golang.org/x/crypto/bcrypt"
)

const (
	fixturePassword = "fixture-password"
	fixtureAPIKey   = "fc_fixture_cross_language_token"
)

func main() {
	if len(os.Args) != 3 {
		panic("usage: fixturegen OUTPUT_DB OUTPUT_MANIFEST")
	}
	dbPath, manifestPath := os.Args[1], os.Args[2]
	_ = os.Remove(dbPath)

	ctx := context.Background()
	st, err := store.NewDBStore("sqlite", dbPath)
	must(err)
	defer st.Close()
	must(st.Migrate(ctx))

	passwordHash, err := bcrypt.GenerateFromPassword([]byte(fixturePassword), bcrypt.DefaultCost)
	must(err)
	now := time.Date(2026, 1, 2, 3, 4, 5, 123000000, time.UTC)
	must(st.CreateUser(ctx, &store.UserRecord{
		ID: "u_go_fixture", Username: "go-fixture", Email: "fixture@example.invalid",
		PasswordHash: string(passwordHash), DisplayName: "Go Fixture", Role: "user",
		Status: "active", CreatedAt: now, UpdatedAt: now,
	}))
	must(st.SaveAgent(ctx, &store.AgentRecord{
		ID: "agt_go_fixture", UserID: "u_go_fixture", Name: "fixture-agent",
		Config: map[string]interface{}{"model": "fixture/model"}, CreatedAt: now, UpdatedAt: now,
	}))

	apiHash := sha256.Sum256([]byte(fixtureAPIKey))
	must(st.CreateAPIKey(ctx, &store.APIKeyRecord{
		ID: "k_go_fixture", UserID: "u_go_fixture", Name: "fixture-key",
		KeyHash: hex.EncodeToString(apiHash[:]), KeyPrefix: "fc_fixtu", CreatedAt: now,
	}))
	must(st.SetAPIKeyAgents(ctx, "k_go_fixture", []string{"agt_go_fixture"}))

	must(st.SaveConfig(ctx, &store.ConfigRecord{
		ID: "cfg_go_channel", Kind: store.KindChannel, Scope: store.ScopeUser,
		ScopeID: "u_go_fixture", Name: "telegram", Enabled: true,
		CredentialKey: "fixture-tail", Data: map[string]interface{}{
			"enabled": true, "botToken": "fixture-bot-token",
			"accounts": map[string]interface{}{
				"primary": map[string]interface{}{"botToken": "fixture-account-token"},
			},
		}, CreatedAt: now, UpdatedAt: now,
	}))

	raw := json.RawMessage(`{"role":"assistant","content":[{"type":"thinking","thinking":"fixture reasoning","signature":"fixture-signature"},{"type":"tool_use","id":"tool-go-1","name":"lookup","input":{"q":"FastClaw"}}]}`)
	adapter := session.NewStoreAdapter(st, "u_go_fixture")
	must(adapter.SaveSession(ctx, "agt_go_fixture", "web_go_fixture", []provider.Message{
		{Role: "user", Content: "Use the lookup tool", Timestamp: now.UnixMilli()},
		{
			Role: "assistant", Thinking: "fixture reasoning", Timestamp: now.UnixMilli(),
			ToolCalls: []provider.ToolCall{{
				ID: "tool-go-1", Type: "function",
				Function: provider.FunctionCall{Name: "lookup", Arguments: `{"q":"FastClaw"}`},
			}}, RawAssistant: raw,
		},
		{Role: "tool", Content: "fixture result", ToolCallID: "tool-go-1", Timestamp: now.UnixMilli()},
	}))
	must(st.SaveAgentFile(ctx, "agt_go_fixture", "u_go_fixture", "SOUL.md", []byte("Fixture agent\n")))

	manifest := map[string]interface{}{
		"referenceCommit": "792417b86b5c12af1b99364865217a74f4d52f38",
		"fixturePassword": fixturePassword,
		"fixtureAPIKey": fixtureAPIKey,
		"userId": "u_go_fixture", "agentId": "agt_go_fixture",
		"apiKeyId": "k_go_fixture", "sessionKey": "web_go_fixture",
		"channelConfigId": "cfg_go_channel",
	}
	data, err := json.MarshalIndent(manifest, "", "  ")
	must(err)
	must(os.WriteFile(manifestPath, append(data, '\n'), 0o644))
	fmt.Println(dbPath)
}

func must(err error) {
	if err != nil {
		panic(err)
	}
}
