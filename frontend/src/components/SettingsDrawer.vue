<template>
  <n-drawer :show="show" :auto-focus="false" @update:show="(val: boolean) => emit('update:show', val)" width="800">
    <n-drawer-content title="设置" closable>
      <n-tabs default-value="model">
        <n-tab-pane name="model" tab="模型管理">
          <n-tabs type="segment" default-value="workbench" size="small">
            <!-- 工作台模型配置（原模型管理） -->
            <n-tab-pane name="workbench" tab="工作台模型配置">
              <n-space vertical>
                <!-- 当前活跃模型指示 -->
                <n-alert v-if="!configStore.activeModel" type="warning" title="尚未选择模型" />
                <div v-else>
                  <n-tag type="info" size="small">当前使用：{{ configStore.activeModel.name }}</n-tag>
                </div>

                <n-divider />

                <!-- 模型列表 -->
                <n-list hoverable clickable>
                  <n-list-item v-for="model in configStore.savedModels" :key="model.id">
                    <template #suffix>
                      <n-space>
                        <n-button text size="small" @click="editModel(model)">编辑</n-button>
                        <n-popconfirm
                        @positive-click="() => configStore.deleteModel(model.id)"
                        negative-text="取消"
                        positive-text="好的"
                        :negative-button-props="{size: 'tiny'}"
                        :positive-button-props="{size: 'tiny'}"
                        >
                          <template #trigger>
                            <n-button text size="small" type="error">删除</n-button>
                          </template>
                          确定删除模型「{{ model.name }}」吗？
                        </n-popconfirm>
                      </n-space>
                    </template>
                    <div>
                      <n-text strong>{{ model.name }}</n-text>
                      <n-text depth="3"> · {{ model.type === 'local' ? '本地' : '云端' }}</n-text>
                      <br />
                      <n-text depth="3" style="font-size: 0.8rem">{{ model.modelName }}</n-text>
                    </div>
                  </n-list-item>
                </n-list>

                <n-button type="primary" block @click="openAddModelDialog">添加模型</n-button>
              </n-space>
            </n-tab-pane>

            <!-- 知识库配置（原知识库设置面板） -->
            <n-tab-pane name="knowledge" tab="知识库配置">
              <KbSettingsPanel />
            </n-tab-pane>
          </n-tabs>
        </n-tab-pane>

        <!-- 技能管理 -->
        <n-tab-pane name="skills" tab="技能管理">
          <n-space vertical>
            <n-text depth="3" style="font-size: 0.8rem">
              可视化注册自定义技能：提示词技能（人人可建）动态注入指令与工具白名单；
              可执行代码技能（仅管理员）动态注册为可调用工具，无需改后端、无需重启。
            </n-text>

            <n-space align="center" :size="8">
              <n-text depth="3" style="font-size: 0.8rem">当前身份：</n-text>
              <n-tag :type="skillStore.userRole === 'admin' ? 'success' : 'default'" size="small">
                {{ skillStore.userRole === 'admin' ? '管理员' : '普通用户' }}
              </n-tag>
            </n-space>

            <n-divider />

            <n-space>
              <n-button type="primary" style="flex: 1" @click="openAddSkillDialog">注册技能</n-button>
              <n-button style="flex: 1" @click="triggerImportSkill" :loading="importing">导入压缩包</n-button>
            </n-space>
            <input
              ref="skillFileInput"
              type="file"
              accept=".zip"
              style="display: none"
              @change="onImportSkillFile"
            />

            <n-grid :cols="2" :x-gap="12" :y-gap="12">
              <n-gi v-for="skill in pagedSkills" :key="skill.id">
                <n-card size="small" hoverable class="item-card">
                  <template #header>
                    <n-space align="center" :size="6">
                      <n-text strong>{{ skill.title }}</n-text>
                      <n-tag size="tiny" round :type="skill.skill_type === 'code' ? 'warning' : 'info'">
                        {{ skill.skill_type === 'code' ? '代码' : '提示词' }}
                      </n-tag>
                      <n-tag v-if="skill.isolated" size="tiny" round>隔离</n-tag>
                    </n-space>
                  </template>
                  <template #header-extra>
                    <n-switch
                      size="small"
                      :value="skill.enabled"
                      @update-value="(v: boolean) => onToggleSkill(skill, v)"
                    />
                  </template>
                  <n-text depth="3" tag="div" style="font-size: 0.75rem; word-break: break-all">
                    {{ skill.name }}{{ skill.description ? ' · ' + skill.description : '' }}
                  </n-text>
                  <template #action>
                    <n-space align="center" justify="end">
                      <n-button text size="small" @click="editSkill(skill)"
                        :disabled="skill.skill_type === 'code' && !skillStore.isAdmin()">编辑</n-button>
                      <n-button text size="small" @click="onExportSkill(skill)">导出</n-button>
                      <n-popconfirm
                        @positive-click="() => onDeleteSkill(skill)"
                        negative-text="取消" positive-text="好的"
                        :negative-button-props="{size: 'tiny'}"
                        :positive-button-props="{size: 'tiny'}"
                      >
                        <template #trigger>
                          <n-button text size="small" type="error"
                            :disabled="skill.skill_type === 'code' && !skillStore.isAdmin()">删除</n-button>
                        </template>
                        确定删除技能「{{ skill.title }}」吗？
                      </n-popconfirm>
                    </n-space>
                  </template>
                </n-card>
              </n-gi>
            </n-grid>
            <div v-if="skillStore.skills.length > PAGE_SIZE" style="display: flex; justify-content: center; margin-top: 12px;">
              <n-pagination v-model:page="skillPage" :page-size="PAGE_SIZE" :item-count="skillStore.skills.length" />
            </div>
            <p v-if="skillStore.skills.length === 0" style="color: gray; font-size: 0.85rem;">暂无技能，点击上方按钮注册。</p>
          </n-space>
        </n-tab-pane>

        <!-- MCP 接入 -->
        <n-tab-pane name="mcp" tab="MCP接入">
          <n-space vertical>
            <n-text depth="3" style="font-size: 0.8rem">
              配置 MCP 服务后，其提供的工具会自动加入系统工具列表，可在角色管理的「赋予能力」中勾选。
            </n-text>

            <n-divider />

            <n-button type="primary" block @click="openAddMcpDialog">添加 MCP 服务</n-button>

            <n-grid :cols="2" :x-gap="12" :y-gap="12">
              <n-gi v-for="server in pagedMcpServers" :key="server.name">
                <n-card size="small" hoverable class="item-card">
                  <template #header>
                    <n-space align="center" :size="6">
                      <n-text strong>{{ server.name }}</n-text>
                      <n-tag :type="server.connected ? 'success' : 'error'" size="tiny" round>
                        {{ server.connected ? '已连接' : '未连接' }}
                      </n-tag>
                    </n-space>
                  </template>
                  <n-text depth="3" tag="div" style="font-size: 0.75rem">
                    {{ server.transport === 'stdio' ? '本地' : '远程' }} · {{ server.tools.length }} 个工具
                  </n-text>
                  <n-text depth="3" tag="div" style="font-size: 0.75rem; word-break: break-all; margin-top: 4px;">
                    {{ server.transport === 'stdio' ? (server.command + ' ' + (server.args || []).join(' ')) : server.url }}
                  </n-text>
                  <template #action>
                    <n-space align="center" justify="end">
                      <n-button text size="small" @click="editMcpServer(server)">编辑</n-button>
                      <n-popconfirm
                        @positive-click="() => deleteMcpServer(server.name)"
                        negative-text="取消"
                        positive-text="好的"
                        :negative-button-props="{size: 'tiny'}"
                        :positive-button-props="{size: 'tiny'}"
                      >
                        <template #trigger>
                          <n-button text size="small" type="error">删除</n-button>
                        </template>
                        确定删除 MCP 服务「{{ server.name }}」吗？
                      </n-popconfirm>
                    </n-space>
                  </template>
                </n-card>
              </n-gi>
            </n-grid>
            <div v-if="mcpStore.servers.length > PAGE_SIZE" style="display: flex; justify-content: center; margin-top: 12px;">
              <n-pagination v-model:page="mcpPage" :page-size="PAGE_SIZE" :item-count="mcpStore.servers.length" />
            </div>
            <p v-if="mcpStore.servers.length === 0" style="color: gray; font-size: 0.85rem;">暂无 MCP 服务，点击上方按钮添加。</p>
          </n-space>
        </n-tab-pane>

        <!-- 知识库设置已并入「模型管理 → 知识库配置」 -->

        <!-- 其他设置 -->
        <n-tab-pane name="function" tab="功能设置">
          <n-form label-placement="left" label-width="80">
            <n-form-item label="启用角色">
              <n-switch v-model:value="chatStore.enableProfile" @update-value="handleProfile"/>
            </n-form-item>
            <n-form-item label="主题">
              <n-button @click="configStore.toggleTheme">
                <template #icon>
                  <n-icon><m-svg :name="configStore.themeMode === 'dark' ? 'moon' : 'son'"/></n-icon>
                </template>
                {{ configStore.themeMode === 'dark' ? '暗色' : '浅色' }}
              </n-button>
            </n-form-item>
            <n-form-item label="工作目录">
              <n-flex>
                <n-input style="width:70%" v-model:value="workspacePath" placeholder="选择或输入目录路径" @change="saveWorkspace(workspacePath)"/>
                <n-button text @click="selectFolder">选择目录</n-button>
              </n-flex>
            </n-form-item>
            <n-form-item label="当前身份">
              <n-flex align="center">
                <n-radio-group :value="skillStore.userRole" @update-value="onChangeUserRole">
                  <n-radio value="admin">管理员</n-radio>
                  <n-radio value="user">普通用户</n-radio>
                </n-radio-group>
              </n-flex>
            </n-form-item>
            <n-form-item label=" " :show-feedback="false">
              <n-text depth="3" style="font-size: 0.72rem">
                管理员可创建/编辑「可执行代码技能」；普通用户仅能使用与创建「提示词技能」。
              </n-text>
            </n-form-item>
          </n-form>

          <!-- ========== 角色管理 ========== -->
          <div v-if="chatStore.enableProfile">
            <n-divider />
            <h3 style="margin-bottom: 12px;">角色管理</h3>
            <n-select
              v-model:value="profileId"
              :options="profileOptions"
              placeholder="选择角色"
              clearable
              style="margin-bottom: 12px;"
            />
            <n-space>
              <n-button @click="openCreateProfile" size="small" secondary type="primary">新建角色</n-button>
              <n-button @click="openEditProfile" size="small" secondary :disabled="!profileStore.activeProfile">编辑</n-button>
              <n-popconfirm
                @positive-click="deleteCurrentProfile"
              >
                <template #trigger>
                  <n-button size="small" secondary type="error" :disabled="!profileStore.activeProfile">删除</n-button>
                </template>
                确定删除当前角色吗？
              </n-popconfirm>
              
            </n-space>
          </div>
        </n-tab-pane>
      </n-tabs>
      <div style="font-size:.6rem;color:#666;width:100%;text-align:center;position: absolute;left:0;right:10px;bottom: 6px">版本：{{ version }}</div>
    </n-drawer-content>
  </n-drawer>

  <!-- 新增/编辑模型对话框 -->
  <n-modal v-model:show="showModelDialog" :auto-focus="false" preset="dialog" draggable :mask-closable="false" :loading="configStore.loading" :title="editingModelId ? '编辑模型' : '添加模型'"
    positive-text="保存" negative-text="取消" @positive-click="saveModel">
    <n-form :model="modelForm" label-placement="left" label-width="80">
      <n-form-item label="名称" required>
        <n-input v-model:value="modelForm.name" placeholder="例如：我的 GPT-4" />
      </n-form-item>
      <n-form-item label="类型">
        <n-radio-group v-model:value="modelForm.type">
          <n-radio value="local">本地模型</n-radio>
          <n-radio value="online">线上模型</n-radio>
        </n-radio-group>
      </n-form-item>
      <n-form-item label="模型 ID" required>
        <n-space>
          <n-select
            style="width: 240px"
            v-model:value="modelForm.modelName"
            :options="modelOptions"
            filterable
            placeholder="请选择或输入模型名称"
            :loading="modelsLoading"
            @focus="autoFetchModels"
          />
          <n-button @click="fetchModels">获取</n-button>
        </n-space>
      </n-form-item>
      <n-form-item label="Base URL">
        <n-input v-model:value="modelForm.baseUrl" placeholder="http://localhost:1234/v1" />
      </n-form-item>
      <n-form-item label="API Key">
        <n-input v-model:value="modelForm.apiKey" type="password" placeholder="sk-..." />
      </n-form-item>
    </n-form>
  </n-modal>

  <!-- 新增/编辑 MCP 服务对话框 -->
  <n-modal v-model:show="showMcpDialog" :auto-focus="false" preset="dialog" draggable :mask-closable="false"
    :loading="mcpSaving" :title="editingMcpName ? '编辑 MCP 服务' : '添加 MCP 服务'"
    positive-text="保存" negative-text="取消" @positive-click="saveMcpServer">
    <n-form :model="mcpForm" label-placement="left" label-width="80">
      <n-form-item label="服务名称" required>
        <n-input v-model:value="mcpForm.name" placeholder="例如：filesystem（唯一标识）" :disabled="!!editingMcpName" />
      </n-form-item>
      <n-form-item label="接入方式">
        <n-radio-group v-model:value="mcpForm.transport">
          <n-radio value="http">远程服务 (URL)</n-radio>
          <n-radio value="stdio">本地命令 (stdio)</n-radio>
        </n-radio-group>
      </n-form-item>
      <n-form-item v-if="mcpForm.transport === 'http'" label="URL" required>
        <n-input v-model:value="mcpForm.url" placeholder="https://example.com/mcp 或 /sse" />
      </n-form-item>
      <template v-else>
        <n-form-item label="命令" required>
          <n-input v-model:value="mcpForm.command" placeholder="例如：npx、python、uvx" />
        </n-form-item>
        <n-form-item label="参数">
          <n-input v-model:value="mcpArgsText" type="textarea"
            placeholder="每行一个参数，例如：&#10;-y&#10;@modelcontextprotocol/server-filesystem&#10;/path/to/dir"
            :autosize="{ minRows: 2, maxRows: 6 }" />
        </n-form-item>
      </template>
    </n-form>
  </n-modal>

  <!-- 新建/编辑角色模态框 -->
  <n-modal 
  v-model:show="profileModalVisible" 
  :auto-focus="false"
  preset="dialog" 
  draggable
  :mask-closable="false"
  style="max-width:520px;width:96%;" 
  :title="isEditing ? '编辑角色' : '新建角色'"
  positive-text="确认"
  negative-text="关闭"
  @positive-click="saveProfile"
  @negative-click="profileModalVisible = false">
    <n-form :model="profileForm" label-placement="left" label-width="70">
      <n-form-item label="角色名称">
        <n-input v-model:value="profileForm.name" placeholder="例如：程序员、生活助手" :maxlength="12" show-count/>
      </n-form-item>
      <n-form-item label="角色描述">
        <n-input
          v-model:value="profileForm.profile_prompt"
          type="textarea"
          placeholder="例如：你是一个专业的 Python 开发者，回答要简洁..."
          show-count
          :maxlength="300"
          :autosize="{ minRows: 3, maxRows: 8 }"
        />
      </n-form-item>
      <n-form-item label="赋予能力">
        <n-button size="small" style="position:absolute;left:-64px;top:40px;" @click="loadTools">刷新</n-button>
        <div style="width: 420px; max-height: 200px; overflow-y: auto;">
          <n-checkbox-group v-model:value="profileForm.tools">
            <n-space vertical>
              <n-checkbox v-for="tool in allTools" :key="tool.function.name" :value="tool.function.name">
                <n-popover trigger="hover" placement="right" :width="400">
                  <template #trigger>
                    <span style="cursor: pointer;">{{tool.function.title}} - {{ tool.function.name }}</span>
                  </template>
                  <div style="word-break: break-word; white-space: pre-wrap;">
                    {{ tool.function.description }}
                  </div>
                </n-popover>
              </n-checkbox>
            </n-space>
          </n-checkbox-group>
        </div>
        <p v-if="allTools.length === 0" style="color: gray;">暂无可用工具，请检查 MCP 服务或工具配置。</p>
      </n-form-item>
      <n-form-item label="选择技能">
        <div style="width: 420px; max-height: 160px; overflow-y: auto;">
          <n-checkbox-group v-model:value="profileForm.skills">
            <n-space vertical>
              <n-checkbox v-for="skill in enabledSkills" :key="skill.name" :value="skill.name">
                <n-popover trigger="hover" placement="right" :width="360">
                  <template #trigger>
                    <span style="cursor: pointer;">
                      {{ skill.title }}
                      <n-tag size="tiny" round :type="skill.skill_type === 'code' ? 'warning' : 'info'">
                        {{ skill.skill_type === 'code' ? '代码' : '提示词' }}
                      </n-tag>
                    </span>
                  </template>
                  <div style="word-break: break-word; white-space: pre-wrap;">
                    {{ skill.description || skill.instruction || '（无描述）' }}
                  </div>
                </n-popover>
              </n-checkbox>
            </n-space>
          </n-checkbox-group>
        </div>
        <p v-if="enabledSkills.length === 0" style="color: gray;">暂无启用的技能，可在「技能管理」中注册。</p>
      </n-form-item>
    </n-form>
    <n-collapse :default-expanded-names="[]">
      <n-collapse-item title="高级设置" name="params">
        <!-- Temperature -->
        <n-form-item label="温度" label-placement="left" label-width="100">
          <n-space align="center">
            <n-slider
              v-model:value="profileForm.temperature"
              :min="0"
              :max="2"
              :step="0.1"
              style="width: 200px"
            />
            <n-input-number
              v-model:value="profileForm.temperature"
              size="small"
              :min="0"
              :max="2"
              :step="0.1"
              style="width: 100px"
            />
          </n-space>
        </n-form-item>

        <!-- Top P -->
        <n-form-item label="Top P采样" label-placement="left" label-width="100">
          <n-space align="center" style="width:100%">
            <n-slider
              v-model:value="profileForm.top_p"
              :min="0"
              :max="1"
              :step="0.05"
              style="width: 200px"
            />
            <n-input-number
              v-model:value="profileForm.top_p"
              size="small"
              :min="0"
              :max="1"
              :step="0.05"
              style="width: 100px"
            />
          </n-space>
        </n-form-item>

        <!-- Top K -->
        <n-form-item label="Top K采样" label-placement="left" label-width="100">
          <n-space align="center">
            <n-slider
              v-model:value="profileForm.top_k"
              :min="1"
              :max="100"
              :step="1"
              style="width: 200px"
            />
            <n-input-number
              v-model:value="profileForm.top_k"
              size="small"
              :min="1"
              :max="100"
              :step="1"
              style="width: 100px"
            />
          </n-space>
        </n-form-item>
        <!-- Frequency Penalty -->
        <n-form-item label="频率惩罚" label-placement="left" label-width="100">
          <n-space align="center">
            <n-slider
              v-model:value="profileForm.frequency_penalty"
              :min="-2"
              :max="2"
              :step="0.1"
              style="width: 200px"
            />
            <n-input-number
              v-model:value="profileForm.frequency_penalty"
              size="small"
              :min="-2"
              :max="2"
              :step="0.1"
              style="width: 100px"
            />
          </n-space>
        </n-form-item>

        <!-- Presence Penalty -->
        <n-form-item label="存在惩罚" label-placement="left" label-width="100">
          <n-space align="center">
            <n-slider
              v-model:value="profileForm.presence_penalty"
              :min="-2"
              :max="2"
              :step="0.1"
              style="width: 200px"
            />
            <n-input-number
              v-model:value="profileForm.presence_penalty"
              size="small"
              :min="-2"
              :max="2"
              :step="0.1"
              style="width: 100px"
            />
          </n-space>
        </n-form-item>
      </n-collapse-item>
    </n-collapse>
  </n-modal>

  <!-- 注册/编辑技能模态框 -->
  <n-modal
    v-model:show="skillModalVisible"
    :auto-focus="false"
    preset="dialog"
    draggable
    :mask-closable="false"
    style="max-width:560px;width:96%;"
    :title="editingSkillId ? '编辑技能' : '注册技能'"
    positive-text="保存"
    negative-text="取消"
    @positive-click="saveSkill"
  >
    <n-form :model="skillForm" label-placement="left" label-width="80">
      <n-form-item label="显示名" required>
        <n-input v-model:value="skillForm.title" placeholder="例如：周报生成器" :maxlength="30" />
      </n-form-item>
      <n-form-item label="标识" required>
        <n-input v-model:value="skillForm.name" placeholder="唯一标识，字母/数字/下划线/连字符，如 weekly-report"
          :disabled="!!editingSkillId" />
      </n-form-item>
      <n-form-item label="说明">
        <n-input v-model:value="skillForm.description" type="textarea" placeholder="技能用途的简要描述"
          :autosize="{ minRows: 1, maxRows: 3 }" />
      </n-form-item>
      <n-form-item label="类型">
        <n-radio-group v-model:value="skillForm.skill_type">
          <n-radio value="prompt">提示词技能</n-radio>
          <n-radio value="code" :disabled="!skillStore.isAdmin()">可执行代码技能</n-radio>
        </n-radio-group>
      </n-form-item>

      <!-- prompt 型：指令 + 工具白名单 -->
      <template v-if="skillForm.skill_type === 'prompt'">
        <n-form-item label="技能指令">
          <n-input v-model:value="skillForm.instruction" type="textarea"
            placeholder="启用后注入到系统提示的指令，指导模型如何运用此技能"
            :autosize="{ minRows: 3, maxRows: 8 }" />
        </n-form-item>
        <n-form-item label="工具白名单">
          <div style="width: 100%; max-height: 160px; overflow-y: auto;">
            <n-checkbox-group v-model:value="skillForm.tools">
              <n-space vertical>
                <n-checkbox v-for="tool in allTools" :key="tool.function.name" :value="tool.function.name">
                  <span style="cursor: pointer;">{{ tool.function.title }} - {{ tool.function.name }}</span>
                </n-checkbox>
              </n-space>
            </n-checkbox-group>
          </div>
        </n-form-item>
      </template>

      <!-- code 型：参数 Schema + 源码（仅管理员） -->
      <template v-else>
        <n-alert type="warning" size="small" style="margin-bottom:8px">
          代码技能会在受限沙箱中执行，仅管理员可编辑。请勿粘贴不可信代码。
        </n-alert>
        <n-form-item label="参数Schema">
          <n-input v-model:value="skillParamsText" type="textarea"
            placeholder='JSON Schema，例如：{"type":"object","properties":{"x":{"type":"number"}},"required":["x"]}'
            :autosize="{ minRows: 2, maxRows: 6 }" />
        </n-form-item>
        <n-form-item label="Python代码">
          <n-input v-model:value="skillForm.code" type="textarea"
            placeholder="必须定义 run(**kwargs)，返回值将回传给模型。可用 json/math/re 等安全模块。"
            :autosize="{ minRows: 4, maxRows: 12 }" />
        </n-form-item>
      </template>

      <n-form-item label="上下文隔离">
        <n-switch v-model:value="skillForm.isolated" />
        <n-text depth="3" style="font-size:0.72rem;margin-left:8px">独立命名空间执行，不共享状态</n-text>
      </n-form-item>
      <n-form-item label="启用">
        <n-switch v-model:value="skillForm.enabled" />
      </n-form-item>
    </n-form>
  </n-modal>
</template>

<script setup lang="ts">
import { ref, reactive, watch, onMounted, computed } from 'vue'
import {
  NDrawer, NDrawerContent, NForm, NFormItem, NInput, NPopover, NFlex,
  NRadioGroup, NRadio, NSwitch, NButton, NSpace, NDivider, NIcon,
  NTabs, NTabPane, NList, NListItem, NPopconfirm, NTag, NAlert,
  NModal, NSelect, NCheckboxGroup, NCheckbox, NText, useMessage, useDialog, NSlider,
  NInputNumber, NCollapseItem, NCollapse, NGrid, NGi, NCard, NPagination
} from 'naive-ui'
import { useChatStore } from '@/stores/chat'
import { useConfigStore, type ModelConfig } from '@/stores/config'
import { useProfileStore, type Profile } from '@/stores/profiles'
import { useMcpStore, type MCPServer } from '@/stores/mcp'
import { useSkillStore, type Skill, type SkillType } from '@/stores/skills'
import mSvg from '@/components/mSvg.vue'
import KbSettingsPanel from '@/components/kb/KbSettingsPanel.vue'


const props = defineProps<{ show: boolean }>()
const emit = defineEmits<{ 'update:show': [value: boolean] }>()

const message = useMessage()
const dialog = useDialog()
const chatStore = useChatStore()
const configStore = useConfigStore()
const profileStore = useProfileStore()
const mcpStore = useMcpStore()
const skillStore = useSkillStore()
const version = ref(import.meta.env.VITE_APP_VERSION)
const profileId = ref()

// 对话框状态
const showModelDialog = ref(false)
const editingModelId = ref<string | null>(null)

const enabledSkills = computed(() => skillStore.skills.filter(s => s.enabled))

// ---------- 卡片分页（每行 2 个，每页 2 行 = 每页 4 个） ----------
const PAGE_SIZE = 4
const skillPage = ref(1)
const mcpPage = ref(1)
const pagedSkills = computed(() =>
  skillStore.skills.slice((skillPage.value - 1) * PAGE_SIZE, skillPage.value * PAGE_SIZE)
)
const pagedMcpServers = computed(() =>
  mcpStore.servers.slice((mcpPage.value - 1) * PAGE_SIZE, mcpPage.value * PAGE_SIZE)
)

watch(() => props.show, (val) => {
  if (val) {
    profileId.value = profileStore.activeProfileId
    skillPage.value = 1
    mcpPage.value = 1
    mcpStore.loadServers()
    skillStore.loadSkills()
    skillStore.loadUserRole()
  }
})

watch(() => profileStore.activeProfileId, (val) => {
  profileId.value = val
})

const modelForm = reactive<{
  name: string
  type: 'local' | 'online'
  modelName?: string
  baseUrl: string
  apiKey: string
}>({
  name: '',
  type: 'local',
  modelName: undefined,
  baseUrl: '',
  apiKey: ''
})

function handleProfile(val: boolean) {
  localStorage.setItem('enableProfile', val.toString())
}

function openAddModelDialog() {
  editingModelId.value = null
  modelForm.name = ''
  modelForm.type = 'local'
  modelForm.modelName = undefined
  modelForm.baseUrl = ''
  modelForm.apiKey = ''
  showModelDialog.value = true
}

function editModel(model: ModelConfig) {
  editingModelId.value = model.id
  modelForm.name = model.name
  modelForm.type = model.type
  modelForm.modelName = model.modelName
  modelForm.baseUrl = model.baseUrl
  modelForm.apiKey = model.apiKey || ''
  showModelDialog.value = true
}

const modelOptions = ref<{ label: string; value: string }[]>([])
const modelsLoading = ref(false)

async function fetchModels() {
  if (modelsLoading.value) return
  if (!modelForm.baseUrl) {
    message.warning('请先填写 Base URL')
    return
  }
  modelsLoading.value = true
  try {
    const res = await fetch('/api/model', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        base_url: modelForm.baseUrl,
        api_key: modelForm.apiKey || ''
      })
    })
    const data = await res.json()
    if(data.detail) {
      if (data.detail.indexOf('timed out') !== -1) {
        message.error('请求超时，请检查网络')
      } else {
        message.error("请正确填写API Key")
      }
      modelOptions.value = []
      return
    }
    modelOptions.value = data.map((id: string) => ({ label: id, value: id }))
    if (data.length === 0) {
      message.info('未检测到可用模型，请检查服务或手动输入')
    }
  } catch (e) {
    message.error('获取模型列表失败')
  } finally {
    modelsLoading.value = false
  }
}

// 当 baseUrl 或 apiKey 改变时，可自动刷新（可选）
watch(() => [modelForm.baseUrl, modelForm.apiKey], () => {
  modelOptions.value = []
})

// 在打开添加/编辑对话框时，若已有 baseUrl 可自动拉取
watch(showModelDialog, (show) => {
  if (show && modelForm.baseUrl) {
    autoFetchModels()
  }
})

function autoFetchModels() {
  if (!modelOptions.value.length && modelForm.baseUrl) {
    fetchModels()
  }
}

function saveModel() {
  if ( !modelForm.modelName || (!modelForm.name.trim() || !modelForm.modelName.trim())) {
    message.warning('名称和模型 ID 不能为空')
    return false
  }
  if (editingModelId.value) {
    configStore.updateModel(editingModelId.value, { ...modelForm })
  } else {
    configStore.addModel({ ...modelForm })
  }
  showModelDialog.value = false
}

const workspacePath = ref(localStorage.getItem('workspacePath') || '')
async function selectFolder() {
  try {
    const folder = await window.pywebview.api.select_folder()
    if (folder) {
      workspacePath.value = folder
      localStorage.setItem('workspacePath', folder)
      await saveWorkspace(folder)
    }
  } catch {
   message.warning('文件夹选择仅支持桌面环境')
  }
}

async function getWorkspace() {
  try {
    const res = await fetch('/api/workspace')
    const data = await res.json()
    if (data.path) {
      workspacePath.value = data.path
      localStorage.setItem('workspacePath', data.path)
    }
  } catch (e) {
    console.warn('获取工作目录失败', e)
  }
}

async function saveWorkspace(path: string, isMsg: boolean = true) {
  await fetch('/api/workspace/set', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path })
  }).then(async (res: any) => {
    if (res.ok) {
      if (isMsg)
        message.success('工作目录设置成功')
      localStorage.setItem('workspacePath', path)
    } else {
      const errorData = await res.json()
      if (isMsg)
        message.error(errorData.detail || '工作目录设置失败')
    }
  })
}

// ---------- MCP 接入 ----------
const showMcpDialog = ref(false)
const mcpSaving = ref(false)
const editingMcpName = ref<string | null>(null)
const mcpForm = reactive<{
  name: string
  transport: 'http' | 'stdio'
  url: string
  command: string
}>({
  name: '',
  transport: 'http',
  url: '',
  command: ''
})
// 参数用多行文本编辑，保存时按行拆分
const mcpArgsText = ref('')

function openAddMcpDialog() {
  editingMcpName.value = null
  mcpForm.name = ''
  mcpForm.transport = 'http'
  mcpForm.url = ''
  mcpForm.command = ''
  mcpArgsText.value = ''
  showMcpDialog.value = true
}

function editMcpServer(server: MCPServer) {
  editingMcpName.value = server.name
  mcpForm.name = server.name
  mcpForm.transport = server.transport
  mcpForm.url = server.url || ''
  mcpForm.command = server.command || ''
  mcpArgsText.value = (server.args || []).join('\n')
  showMcpDialog.value = true
}

async function saveMcpServer() {
  if (!mcpForm.name.trim()) {
    message.warning('服务名称不能为空')
    return false
  }
  if (mcpForm.transport === 'http' && !mcpForm.url.trim()) {
    message.warning('请填写远程服务 URL')
    return false
  }
  if (mcpForm.transport === 'stdio' && !mcpForm.command.trim()) {
    message.warning('请填写本地命令')
    return false
  }

  const payload = {
    name: mcpForm.name.trim(),
    transport: mcpForm.transport,
    url: mcpForm.transport === 'http' ? mcpForm.url.trim() : undefined,
    command: mcpForm.transport === 'stdio' ? mcpForm.command.trim() : undefined,
    args: mcpForm.transport === 'stdio'
      ? mcpArgsText.value.split('\n').map(s => s.trim()).filter(Boolean)
      : undefined
  }

  mcpSaving.value = true
  try {
    const result = await mcpStore.saveServer(payload)
    if (result.connected) {
      message.success(`连接成功，发现 ${result.tools.length} 个工具`)
      showMcpDialog.value = false
      // 刷新工具列表，使新工具可在角色「赋予能力」中勾选
      loadTools()
    } else {
      message.error(`已保存，但连接失败：${result.error || '未知错误'}`)
      // 连接失败仍关闭弹窗，配置已保存，可稍后编辑重试
      showMcpDialog.value = false
      loadTools()
    }
  } catch (e: any) {
    message.error(e.message || '保存失败')
    return false
  } finally {
    mcpSaving.value = false
  }
}

async function deleteMcpServer(name: string) {
  await mcpStore.deleteServer(name)
  message.success('已删除')
  loadTools()
}

// ---------- 角色管理 ----------
const allTools = ref<{ function: { name: string; title: string; description: string } }[]>([])

// 加载全局工具列表
async function loadTools() {
  try {
    const res = await fetch('/api/tools')
    const data = await res.json()
    allTools.value = data.tools || []
  } catch (e) {
    console.warn('获取工具列表失败', e)
  }
}

// 角色相关状态
const profileModalVisible = ref(false)
const isEditing = ref(false)
const editingProfile = ref<Profile | null>(null)
const profileForm = reactive({
  name: '',
  tools: [] as string[],
  skills: [] as string[],
  profile_prompt: '',
  temperature: 1,
  top_p: 1,
  top_k: 40,
  frequency_penalty: 0,
  presence_penalty: 0
})

const profileOptions = computed(() =>
  profileStore.profiles.map(p => ({ label: p.name, value: p.id }))
)

function openCreateProfile() {
  if(allTools.value.length === 0) {
    loadTools()
  }
  skillStore.loadSkills()
  isEditing.value = false
  editingProfile.value = null
  profileForm.name = ''
  profileForm.tools = []
  profileForm.skills = []
  profileForm.profile_prompt = ''
  profileForm.temperature = 1
  profileForm.top_p = 1
  profileForm.top_k = 40
  profileForm.frequency_penalty = 0
  profileForm.presence_penalty = 0
  profileModalVisible.value = true
}

function openEditProfile() {
  if (!profileStore.activeProfile) return
  if(allTools.value.length === 0) {
    loadTools()
  }
  skillStore.loadSkills()
  isEditing.value = true
  const p = profileStore.activeProfile
  editingProfile.value = { ...p }
  profileForm.name = p.name
  profileForm.tools = [...p.tools]
  profileForm.skills = [...(p.skills || [])]
  profileForm.profile_prompt = p.profile_prompt || ''
  // 读取角色保存的参数，若旧角色没有则使用默认值
  profileForm.temperature = p.temperature ?? 1
  profileForm.top_p = p.top_p ?? 1
  profileForm.top_k = p.top_k ?? 40
  profileForm.frequency_penalty = p.frequency_penalty ?? 0
  profileForm.presence_penalty = p.presence_penalty ?? 0
  profileModalVisible.value = true
}

async function saveProfile() {
  if (isEditing.value && editingProfile.value) {
    await profileStore.updateProfile(
      editingProfile.value.id,
      profileForm.name,
      profileForm.tools,
      profileForm.profile_prompt,
      profileForm.temperature,
      profileForm.top_p,
      profileForm.top_k,
      profileForm.frequency_penalty,
      profileForm.presence_penalty,
      profileForm.skills
    )
  } else {
    await profileStore.createProfile(
      profileForm.name,
      profileForm.tools,
      profileForm.profile_prompt,
      profileForm.temperature,
      profileForm.top_p,
      profileForm.top_k,
      profileForm.frequency_penalty,
      profileForm.presence_penalty,
      profileForm.skills
    )
  }
  profileModalVisible.value = false
}

async function deleteCurrentProfile() {
  if (profileStore.activeProfile) {
    await profileStore.deleteProfile(profileStore.activeProfile.id)
  }
}

// ---------- 身份切换 ----------
async function onChangeUserRole(role: 'admin' | 'user') {
  try {
    await skillStore.setUserRole(role)
    message.success(role === 'admin' ? '已切换为管理员' : '已切换为普通用户')
  } catch (e: any) {
    message.error(e.message || '切换失败')
  }
}

// ---------- 技能管理 ----------
const skillModalVisible = ref(false)
const editingSkillId = ref<number | null>(null)
const skillParamsText = ref('{}')

// ---------- 压缩包导入 / 导出 ----------
const skillFileInput = ref<HTMLInputElement | null>(null)
const importing = ref(false)

function triggerImportSkill() {
  skillFileInput.value?.click()
}

async function doImport(file: File, overwrite: boolean) {
  importing.value = true
  try {
    const skill = await skillStore.importSkillPackage(file, overwrite)
    message.success(`已${overwrite ? '覆盖' : '导入'}技能「${skill.title}」`)
  } catch (e: any) {
    if (e.conflict) {
      dialog.warning({
        title: '技能已存在',
        content: '同名技能已存在，是否覆盖？',
        positiveText: '覆盖',
        negativeText: '取消',
        onPositiveClick: () => doImport(file, true)
      })
    } else {
      message.error(e.message || '导入失败')
    }
  } finally {
    importing.value = false
  }
}

async function onImportSkillFile(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''  // 允许重复选择同一文件
  if (!file) return
  await doImport(file, false)
}

function onExportSkill(skill: Skill) {
  const a = document.createElement('a')
  a.href = skillStore.exportSkillUrl(skill.id)
  a.download = `${skill.name}.zip`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
}

const skillForm = reactive<{
  name: string
  title: string
  description: string
  skill_type: SkillType
  enabled: boolean
  instruction: string
  tools: string[]
  code: string
  isolated: boolean
}>({
  name: '', title: '', description: '', skill_type: 'prompt',
  enabled: true, instruction: '', tools: [], code: '', isolated: false
})

function resetSkillForm() {
  skillForm.name = ''
  skillForm.title = ''
  skillForm.description = ''
  skillForm.skill_type = 'prompt'
  skillForm.enabled = true
  skillForm.instruction = ''
  skillForm.tools = []
  skillForm.code = ''
  skillForm.isolated = false
  skillParamsText.value = '{}'
}

function openAddSkillDialog() {
  if (allTools.value.length === 0) loadTools()
  editingSkillId.value = null
  resetSkillForm()
  skillModalVisible.value = true
}

function editSkill(skill: Skill) {
  if (allTools.value.length === 0) loadTools()
  editingSkillId.value = skill.id
  skillForm.name = skill.name
  skillForm.title = skill.title
  skillForm.description = skill.description
  skillForm.skill_type = skill.skill_type
  skillForm.enabled = skill.enabled
  skillForm.instruction = skill.instruction
  skillForm.tools = [...(skill.tools || [])]
  skillForm.code = skill.code
  skillForm.isolated = skill.isolated
  skillParamsText.value = JSON.stringify(skill.parameters || {}, null, 2)
  skillModalVisible.value = true
}

async function saveSkill() {
  if (!skillForm.title.trim()) { message.warning('显示名不能为空'); return false }
  if (!skillForm.name.trim()) { message.warning('标识不能为空'); return false }
  let parameters: Record<string, any> = {}
  if (skillForm.skill_type === 'code') {
    if (!skillForm.code.includes('def run')) {
      message.warning('代码技能必须定义 run(**kwargs) 函数'); return false
    }
    try {
      parameters = skillParamsText.value.trim() ? JSON.parse(skillParamsText.value) : {}
    } catch {
      message.error('参数 Schema 不是合法 JSON'); return false
    }
  }
  const payload = {
    name: skillForm.name.trim(),
    title: skillForm.title.trim(),
    description: skillForm.description,
    skill_type: skillForm.skill_type,
    enabled: skillForm.enabled,
    instruction: skillForm.instruction,
    tools: skillForm.tools,
    code: skillForm.code,
    parameters,
    isolated: skillForm.isolated,
  }
  try {
    await skillStore.saveSkill(payload, editingSkillId.value ?? undefined)
    message.success('已保存')
    skillModalVisible.value = false
  } catch (e: any) {
    message.error(e.message || '保存失败')
    return false
  }
}

async function onToggleSkill(skill: Skill, enabled: boolean) {
  try {
    await skillStore.toggleSkill(skill.id, enabled)
  } catch (e: any) {
    message.error(e.message || '操作失败')
  }
}

async function onDeleteSkill(skill: Skill) {
  try {
    await skillStore.deleteSkill(skill.id)
    message.success('已删除')
  } catch (e: any) {
    message.error(e.message || '删除失败')
  }
}

onMounted(() => {
  configStore.loadModels()
  if (!workspacePath.value) {
    getWorkspace()
  } else {
    saveWorkspace(workspacePath.value, false)
  }
})
</script>

<style scoped>
.n-tab-pane {margin-bottom:20px;}
.item-card {
  height: 100%;
}
</style>