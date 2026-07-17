--[[
Captain.lua — Resolve Free/Studio Scripts host

Holds live `resolve`, serves a file-based JSON bridge, launches the Captain UI.
Does not require bmd.parseJSON (uses a pure-Lua JSON codec).
]]

-- Lightweight log for support/debugging:
-- ~/Library/Application Support/Captain/resolve-script.log
local function script_log(message)
    local path = (os.getenv("HOME") or "")
        .. "/Library/Application Support/Captain/resolve-script.log"
    local f = io.open(path, "a")
    if f then
        f:write(os.date("%Y-%m-%d %H:%M:%S") .. " " .. tostring(message) .. "\n")
        f:close()
    end
end

-- ---- pure Lua JSON (no bmd.parseJSON required) -----------------------------

local function json_escape(s)
    s = tostring(s)
    s = s:gsub("\\", "\\\\")
    s = s:gsub('"', '\\"')
    s = s:gsub("\n", "\\n")
    s = s:gsub("\r", "\\r")
    s = s:gsub("\t", "\\t")
    return s
end

local function is_array(t)
    local n = 0
    for k, _ in pairs(t) do
        if type(k) ~= "number" then
            return false
        end
        if k > n then
            n = k
        end
    end
    for i = 1, n do
        if t[i] == nil then
            return false
        end
    end
    return n > 0 or (next(t) == nil)
end

local function json_encode(val)
    local tv = type(val)
    if val == nil then
        return "null"
    elseif tv == "boolean" then
        return val and "true" or "false"
    elseif tv == "number" then
        return tostring(val)
    elseif tv == "string" then
        return '"' .. json_escape(val) .. '"'
    elseif tv == "table" then
        if is_array(val) then
            local parts = {}
            for i = 1, #val do
                parts[i] = json_encode(val[i])
            end
            return "[" .. table.concat(parts, ",") .. "]"
        else
            local parts = {}
            for k, v in pairs(val) do
                if type(k) == "string" and string.sub(k, 1, 1) ~= "_" then
                    table.insert(parts, '"' .. json_escape(k) .. '":' .. json_encode(v))
                end
            end
            return "{" .. table.concat(parts, ",") .. "}"
        end
    end
    error("Cannot JSON-encode type " .. tv)
end

local function json_decode(str)
    local i = 1
    local s = str

    local function peek()
        return s:sub(i, i)
    end

    local function skip_ws()
        local _, j = s:find("^[ \t\n\r]*", i)
        i = (j or i - 1) + 1
    end

    local parse_value

    local function parse_string()
        i = i + 1
        local out = {}
        while true do
            local c = s:sub(i, i)
            if c == "" then
                error("Unterminated string")
            elseif c == '"' then
                i = i + 1
                return table.concat(out)
            elseif c == "\\" then
                local n = s:sub(i + 1, i + 1)
                local map = { ['"'] = '"', ["\\"] = "\\", ["/"] = "/", b = "\b", f = "\f", n = "\n", r = "\r", t = "\t" }
                if n == "u" then
                    local hex = s:sub(i + 2, i + 5)
                    out[#out + 1] = string.char(tonumber(hex, 16) % 256)
                    i = i + 6
                else
                    out[#out + 1] = map[n] or n
                    i = i + 2
                end
            else
                out[#out + 1] = c
                i = i + 1
            end
        end
    end

    local function parse_number()
        local j = s:find("[^0-9%eE%+%-%.]", i) or (#s + 1)
        local num = tonumber(s:sub(i, j - 1))
        if not num then
            error("Invalid number at " .. i)
        end
        i = j
        return num
    end

    local function parse_array()
        i = i + 1
        local arr = {}
        skip_ws()
        if peek() == "]" then
            i = i + 1
            return arr
        end
        while true do
            arr[#arr + 1] = parse_value()
            skip_ws()
            local c = peek()
            if c == "]" then
                i = i + 1
                return arr
            elseif c == "," then
                i = i + 1
                skip_ws()
            else
                error("Expected , or ] in array at " .. i)
            end
        end
    end

    local function parse_object()
        i = i + 1
        local obj = {}
        skip_ws()
        if peek() == "}" then
            i = i + 1
            return obj
        end
        while true do
            skip_ws()
            if peek() ~= '"' then
                error("Expected string key at " .. i)
            end
            local key = parse_string()
            skip_ws()
            if peek() ~= ":" then
                error("Expected : at " .. i)
            end
            i = i + 1
            obj[key] = parse_value()
            skip_ws()
            local c = peek()
            if c == "}" then
                i = i + 1
                return obj
            elseif c == "," then
                i = i + 1
            else
                error("Expected , or } in object at " .. i)
            end
        end
    end

    parse_value = function()
        skip_ws()
        local c = peek()
        if c == '"' then
            return parse_string()
        elseif c == "{" then
            return parse_object()
        elseif c == "[" then
            return parse_array()
        elseif c == "t" and s:sub(i, i + 3) == "true" then
            i = i + 4
            return true
        elseif c == "f" and s:sub(i, i + 4) == "false" then
            i = i + 5
            return false
        elseif c == "n" and s:sub(i, i + 3) == "null" then
            i = i + 4
            return nil
        elseif c == "-" or c:match("%d") then
            return parse_number()
        end
        error("Unexpected character at " .. i .. ": " .. c)
    end

    local ok, result = pcall(parse_value)
    if not ok then
        error("JSON decode failed: " .. tostring(result))
    end
    return result
end

-- Prefer bmd helpers when present; otherwise pure Lua.
local function decode_json(s)
    if bmd and bmd.parseJSON then
        return bmd.parseJSON(s)
    end
    return json_decode(s)
end

local function encode_json(t)
    if bmd and bmd.encodeJSON then
        return bmd.encodeJSON(t)
    end
    return json_encode(t)
end

-- ---- IO helpers ------------------------------------------------------------

local function data_dir()
    local home = os.getenv("HOME") or ""
    return home .. "/Library/Application Support/Captain"
end

local function read_file(path)
    local f = io.open(path, "r")
    if not f then
        return nil
    end
    local data = f:read("*a")
    f:close()
    return data
end

local function write_file(path, data)
    local tmp = path .. ".tmp"
    local f = io.open(tmp, "w")
    if not f then
        error("Cannot write " .. tmp)
    end
    f:write(data)
    f:close()
    os.rename(tmp, path)
end

local function remove_file(path)
    os.remove(path)
end

local function file_exists(path)
    local f = io.open(path, "r")
    if f then
        f:close()
        return true
    end
    return false
end

local function sleep(seconds)
    if bmd and bmd.wait then
        bmd.wait(seconds)
    else
        local t0 = os.clock()
        while os.clock() - t0 < seconds do
        end
    end
end

local function make_token()
    math.randomseed(os.time() + math.floor(os.clock() * 100000))
    local t = {}
    for i = 1, 32 do
        t[i] = string.format("%x", math.random(0, 15))
    end
    return table.concat(t)
end

local function load_install()
    local path = data_dir() .. "/install.json"
    local raw = read_file(path)
    if not raw then
        error("Captain is not installed. Run setupfiles/install-mac.sh first.\nMissing: " .. path)
    end
    return decode_json(raw)
end

local function mkdir_p(path)
    os.execute(string.format('mkdir -p "%s"', path))
end

-- Resolve API methods sometimes return *no value* (not even nil). Passing that
-- straight into tonumber() raises: bad argument #1 to 'tonumber' (value expected).
local function safe_number(value, default)
    if value == nil then
        return default
    end
    local n = tonumber(value)
    if n == nil then
        return default
    end
    return n
end

-- ---- Resolve helpers -------------------------------------------------------

local clips_by_id = {}

local function version_string()
    local ok, v = pcall(function()
        return resolve:GetVersionString()
    end)
    if ok and v then
        return tostring(v)
    end
    return "unknown"
end

local function current_project()
    local pm = resolve:GetProjectManager()
    local project = pm:GetCurrentProject()
    if not project then
        error("No project is open in Resolve.")
    end
    return project
end

local function current_timeline()
    local timeline = current_project():GetCurrentTimeline()
    if not timeline then
        error("No timeline is open in Resolve.")
    end
    return timeline
end

local function timeline_fps()
    local timeline = current_timeline()
    local fps = safe_number(timeline:GetSetting("timelineFrameRate"), nil)
    if not fps then
        fps = safe_number(current_project():GetSetting("timelineFrameRate"), 24)
    end
    return fps
end

local function frame_to_timecode(frame, fps)
    local fps_i = math.max(1, math.floor(fps + 0.5))
    local ff = frame % fps_i
    local ss = math.floor(frame / fps_i) % 60
    local mm = math.floor(frame / (fps_i * 60)) % 60
    local hh = math.floor(frame / (fps_i * 3600))
    return string.format("%02d:%02d:%02d:%02d", hh, mm, ss, ff)
end

local function list_clips()
    local timeline = current_timeline()
    local fps = timeline_fps()
    clips_by_id = {}
    local out = {}
    for _, track_type in ipairs({ "video", "audio" }) do
        local count = timeline:GetTrackCount(track_type) or 0
        for idx = 1, count do
            local items = timeline:GetItemListInTrack(track_type, idx) or {}
            for _, item in ipairs(items) do
                local mp = item:GetMediaPoolItem()
                local file_path = ""
                if mp then
                    file_path = mp:GetClipProperty("File Path") or ""
                end
                local start_f = safe_number(item:GetStart(), 0)
                local source_start = safe_number(item:GetSourceStartFrame(), 0)
                local clip_id = string.format("%s:%d:%d:%d", track_type, idx, start_f, source_start)
                local clip = {
                    clip_id = clip_id,
                    name = item:GetName() or "",
                    track_type = track_type,
                    track_index = idx,
                    timeline_start_frame = start_f,
                    timeline_end_frame = safe_number(item:GetEnd(), start_f),
                    source_start_frame = source_start,
                    source_end_frame = safe_number(item:GetSourceEndFrame(), source_start),
                    file_path = file_path,
                    fps = fps,
                    _item = item,
                    _mp = mp,
                }
                clips_by_id[clip_id] = clip
                table.insert(out, {
                    clip_id = clip.clip_id,
                    name = clip.name,
                    track_type = clip.track_type,
                    track_index = clip.track_index,
                    timeline_start_frame = clip.timeline_start_frame,
                    timeline_end_frame = clip.timeline_end_frame,
                    source_start_frame = clip.source_start_frame,
                    source_end_frame = clip.source_end_frame,
                    file_path = clip.file_path,
                    fps = clip.fps,
                })
            end
        end
    end
    return out
end

local function clip_under_playhead()
    local timeline = current_timeline()
    local item = timeline:GetCurrentVideoItem()
    if not item then
        error("No video clip under the playhead. Move the playhead over a clip.")
    end
    local listed = list_clips()
    local start_f = safe_number(item:GetStart(), 0)
    local source_start = safe_number(item:GetSourceStartFrame(), 0)
    for _, clip in ipairs(listed) do
        if clip.track_type == "video"
            and clip.timeline_start_frame == start_f
            and clip.source_start_frame == source_start then
            if not clip.file_path or clip.file_path == "" then
                error("Clip '" .. tostring(clip.name) .. "' has no media file path and cannot be transcribed.")
            end
            return clip
        end
    end
    local track_type, track_index = "video", 1
    local ok_ti, info = pcall(function() return item:GetTrackTypeAndIndex() end)
    if ok_ti and info and info[1] and info[2] then
        track_type = tostring(info[1])
        track_index = safe_number(info[2], 1)
    end
    local fps = timeline_fps()
    local mp = item:GetMediaPoolItem()
    local file_path = ""
    if mp then
        file_path = mp:GetClipProperty("File Path") or ""
    end
    if file_path == "" then
        error("Clip '" .. tostring(item:GetName()) .. "' has no media file path and cannot be transcribed.")
    end
    local clip_id = string.format("%s:%d:%d:%d", track_type, track_index, start_f, source_start)
    local clip = {
        clip_id = clip_id,
        name = item:GetName() or "",
        track_type = track_type,
        track_index = track_index,
        timeline_start_frame = start_f,
        timeline_end_frame = safe_number(item:GetEnd(), start_f),
        source_start_frame = source_start,
        source_end_frame = safe_number(item:GetSourceEndFrame(), source_start),
        file_path = file_path,
        fps = fps,
        _item = item,
        _mp = mp,
    }
    clips_by_id[clip_id] = clip
    return {
        clip_id = clip.clip_id,
        name = clip.name,
        track_type = clip.track_type,
        track_index = clip.track_index,
        timeline_start_frame = clip.timeline_start_frame,
        timeline_end_frame = clip.timeline_end_frame,
        source_start_frame = clip.source_start_frame,
        source_end_frame = clip.source_end_frame,
        file_path = clip.file_path,
        fps = clip.fps,
    }
end

local function jump_to_clip_second(clip_id, second_in_clip)
    local clip = clips_by_id[clip_id]
    if not clip then
        error("Unknown clip id " .. tostring(clip_id) .. ". Refresh the clip list.")
    end
    resolve:OpenPage("edit")
    local timeline = current_timeline()
    local source_start_sec = clip.source_start_frame / clip.fps
    local source_offset = second_in_clip - source_start_sec
    local frame = clip.timeline_start_frame + math.floor(source_offset * clip.fps + 0.5)
    if frame < clip.timeline_start_frame then
        frame = clip.timeline_start_frame
    end
    if frame > clip.timeline_end_frame - 1 then
        frame = clip.timeline_end_frame - 1
    end
    timeline:SetCurrentTimecode(frame_to_timecode(frame, clip.fps))
    return true
end

local function import_timeline_xml(xml_path)
    local project = current_project()
    local media_pool = project:GetMediaPool()
    local root = media_pool:GetRootFolder()
    local captain_bin = nil
    local subs = root:GetSubFolderList() or {}
    for _, folder in ipairs(subs) do
        if folder:GetName() == "Captain" then
            captain_bin = folder
            break
        end
    end
    if not captain_bin then
        captain_bin = media_pool:AddSubFolder(root, "Captain")
    end
    if captain_bin then
        media_pool:SetCurrentFolder(captain_bin)
    end
    local timeline = media_pool:ImportTimelineFromFile(xml_path)
    return timeline ~= nil
end

local function assemble_append(clip_id, keep_ranges_frames, new_name)
    local clip = clips_by_id[clip_id]
    if not clip then
        error("Unknown clip id " .. tostring(clip_id))
    end
    if not clip._mp then
        error("Clip has no media pool item; cannot assemble.")
    end
    local project = current_project()
    local media_pool = project:GetMediaPool()
    local timeline = media_pool:CreateEmptyTimeline(new_name)
    if not timeline then
        error("Could not create timeline " .. tostring(new_name))
    end
    project:SetCurrentTimeline(timeline)
    local entries = {}
    for _, range in ipairs(keep_ranges_frames) do
        table.insert(entries, {
            mediaPoolItem = clip._mp,
            startFrame = safe_number(range[1], 0),
            endFrame = safe_number(range[2], 0),
        })
    end
    local i = 1
    while i <= #entries do
        local chunk = {}
        for j = i, math.min(i + 49, #entries) do
            table.insert(chunk, entries[j])
        end
        if not media_pool:AppendToTimeline(chunk) then
            return false
        end
        i = i + 50
    end
    return true
end

local function replace_clip_in_place(clip_id, keep_ranges_frames)
    local clip = clips_by_id[clip_id]
    if not clip then
        list_clips()
        clip = clips_by_id[clip_id]
    end
    if not clip then
        error("Unknown clip id " .. tostring(clip_id) .. ". Refresh the clip list.")
    end
    if not clip._item then
        error("Clip '" .. tostring(clip.name) .. "' is not available on the current timeline.")
    end
    if not clip._mp then
        error("Clip '" .. tostring(clip.name) .. "' has no media pool item; cannot replace.")
    end
    if not keep_ranges_frames or #keep_ranges_frames == 0 then
        error("Nothing left to keep.")
    end
    local timeline = current_timeline()
    local record_frame = safe_number(clip._item:GetStart(), clip.timeline_start_frame)
    local track_index = clip.track_index
    local media_type = 1
    if clip.track_type == "audio" then
        media_type = 2
    end
    if not timeline:DeleteClips({ clip._item }, true) then
        error("Failed to delete clip '" .. tostring(clip.name) .. "' from the timeline.")
    end
    local media_pool = current_project():GetMediaPool()
    local entries = {}
    local rf = record_frame
    for _, range in ipairs(keep_ranges_frames) do
        local start_f = safe_number(range[1], 0)
        local end_f = safe_number(range[2], 0)
        local duration = math.max(0, end_f - start_f)
        table.insert(entries, {
            mediaPoolItem = clip._mp,
            startFrame = start_f,
            endFrame = end_f,
            trackIndex = track_index,
            recordFrame = rf,
            mediaType = media_type,
        })
        rf = rf + duration
    end
    local i = 1
    while i <= #entries do
        local chunk = {}
        for j = i, math.min(i + 49, #entries) do
            table.insert(chunk, entries[j])
        end
        if not media_pool:AppendToTimeline(chunk) then
            return false
        end
        i = i + 50
    end
    clips_by_id = {}
    return true
end

local function dispatch(method, params)
    params = params or {}
    if method == "ping" then
        return { ok = true, version = version_string(), mode = "lua-file" }
    elseif method == "timeline_name" then
        return current_timeline():GetName()
    elseif method == "timeline_fps" then
        return timeline_fps()
    elseif method == "list_clips" then
        return list_clips()
    elseif method == "clip_under_playhead" then
        return clip_under_playhead()
    elseif method == "jump_to_clip_second" then
        return jump_to_clip_second(params.clip_id, safe_number(params.second_in_clip, 0))
    elseif method == "import_timeline_xml" then
        return import_timeline_xml(params.xml_path)
    elseif method == "assemble_append" then
        return assemble_append(params.clip_id, params.keep_ranges_frames, params.new_name)
    elseif method == "replace_clip_in_place" then
        return replace_clip_in_place(params.clip_id, params.keep_ranges_frames)
    else
        error("Unknown bridge method: " .. tostring(method))
    end
end

-- ---- main ------------------------------------------------------------------

script_log("Captain script started (resolve=" .. tostring(resolve ~= nil) .. ")")

local ok_main, err_main = pcall(function()
    if not resolve then
        error("No resolve object. Run from Workspace → Scripts.")
    end

    -- Built-in self-check (replaces the old HelloCaptain probe).
    mkdir_p(data_dir())
    write_file(data_dir() .. "/hello-ok.txt",
        "Captain Scripts host started at " .. os.date("%Y-%m-%d %H:%M:%S") .. "\n" ..
        "Resolve scripting is working.\n")
    print("Captain: Scripts host OK — launching UI...")

    local install = load_install()
    local python = install.python
    local app_dir = install.app_dir
    if not python or not app_dir then
        error("install.json is missing python/app_dir")
    end

    local bridge_dir = data_dir() .. "/bridge"
    mkdir_p(bridge_dir)
    remove_file(bridge_dir .. "/request.json")
    remove_file(bridge_dir .. "/response.json")
    remove_file(bridge_dir .. "/ready.json")

    local token = make_token()
    write_file(bridge_dir .. "/ready.json", encode_json({
        ok = true,
        token = token,
        protocol = 1,
    }))

    local src = app_dir .. "/src"
    local ui_log = data_dir() .. "/ui-launch.log"
    local cmd = string.format(
        'cd "%s" && CAPTAIN_BRIDGE_MODE=file CAPTAIN_BRIDGE_DIR="%s" CAPTAIN_BRIDGE_TOKEN="%s" PYTHONPATH="%s" "%s" -m captain.main > "%s" 2>&1 & echo $!',
        app_dir,
        bridge_dir,
        token,
        src,
        python,
        ui_log
    )

    local handle = io.popen(cmd)
    local pid = handle and handle:read("*l") or ""
    if handle then
        handle:close()
    end
    pid = (pid or ""):gsub("%s+", "")
    script_log("UI spawned pid=" .. tostring(pid))
    if pid == "" then
        error("Failed to launch Captain UI. See " .. ui_log)
    end

    print("Captain Lua bridge ready. UI pid=" .. pid)
    print("Leave this script running until you quit Captain.")

    local authenticated = false
    local running = true
    while running do
        if file_exists(bridge_dir .. "/request.json") then
            local raw = read_file(bridge_dir .. "/request.json")
            remove_file(bridge_dir .. "/request.json")
            local req = decode_json(raw)
            local req_id = req.id
            local method = req.method
            local params = req.params or {}
            local response
            local ok, result_or_err = pcall(function()
                if method == "auth" then
                    if params.token ~= token then
                        error("Invalid bridge token")
                    end
                    authenticated = true
                    return { ok = true, protocol = 1 }
                end
                if not authenticated then
                    error("Not authenticated")
                end
                return dispatch(method, params)
            end)
            if ok then
                response = { id = req_id, result = result_or_err }
            else
                response = { id = req_id, error = { message = tostring(result_or_err) } }
                script_log("bridge request failed: " .. tostring(method)
                    .. " — " .. tostring(result_or_err))
            end
            write_file(bridge_dir .. "/response.json", encode_json(response))
        end

        local ps = io.popen("ps -p " .. pid .. " -o pid= 2>/dev/null")
        local out = ps and ps:read("*a") or ""
        if ps then
            ps:close()
        end
        if not out:match("%d") then
            running = false
        end
        sleep(0.05)
    end

    remove_file(bridge_dir .. "/ready.json")
    remove_file(bridge_dir .. "/request.json")
    remove_file(bridge_dir .. "/response.json")
    print("Captain Lua bridge stopped.")
end)

if not ok_main then
    script_log("FATAL: " .. tostring(err_main))
    print("Captain error: " .. tostring(err_main))
    error(err_main)
end
