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
local list_clips

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

local function timeline_info()
    local timeline = current_timeline()
    local project = current_project()
    local fps = timeline_fps()
    local width = safe_number(timeline:GetSetting("timelineResolutionWidth"), nil)
    local height = safe_number(timeline:GetSetting("timelineResolutionHeight"), nil)
    if not width then
        width = safe_number(project:GetSetting("timelineResolutionWidth"), 1920)
    end
    if not height then
        height = safe_number(project:GetSetting("timelineResolutionHeight"), 1080)
    end
    local tc = ""
    local ok_tc, value = pcall(function() return timeline:GetCurrentTimecode() end)
    if ok_tc and value then
        tc = tostring(value)
    end
    local hh, mm, ss, ff = tc:match("^(%d+):(%d+):(%d+):(%d+)$")
    local playhead = 0
    if hh then
        playhead = ((tonumber(hh) * 3600 + tonumber(mm) * 60 + tonumber(ss))
            * math.floor(fps + 0.5)) + tonumber(ff)
    end
    return {
        fps = fps,
        width = width,
        height = height,
        playhead_timecode = tc,
        playhead_frame = playhead,
    }
end

local function capture_current_frame(image_path)
    local timeline = current_timeline()
    local still = nil
    local album = nil
    local ok, result = pcall(function()
        local gallery = resolve:GetGallery()
        album = gallery and gallery:GetCurrentStillAlbum() or nil
        still = timeline:GrabStill()
        if not still or not album then
            return false
        end
        local folder = image_path:match("^(.*)/[^/]+$") or "."
        local prefix = image_path:match("([^/]+)%.png$") or "captain-preview"
        mkdir_p(folder)
        return album:ExportStills({ still }, folder, prefix, "png")
    end)
    if still and album then
        pcall(function() album:DeleteStills({ still }) end)
    end
    if not ok or not result then
        return nil
    end
    if file_exists(image_path) then
        return image_path
    end
    local function shell_quote(value)
        return "'" .. tostring(value):gsub("'", "'\\''") .. "'"
    end
    local folder = image_path:match("^(.*)/[^/]+$") or "."
    local prefix = image_path:match("([^/]+)%.png$") or "captain-preview"
    local command = "find " .. shell_quote(folder)
        .. " -maxdepth 1 -type f -name " .. shell_quote(prefix .. "_*.png")
        .. " -print -quit"
    local pipe = io.popen(command, "r")
    if not pipe then
        return nil
    end
    local exported_path = pipe:read("*l")
    pipe:close()
    return exported_path
end

local function render_clip_audio(clip_id, output_path)
    local clip = clips_by_id[clip_id]
    if not clip then
        list_clips()
        clip = clips_by_id[clip_id]
    end
    if not clip then
        error("Unknown clip id " .. tostring(clip_id) .. ". Refresh the clip list.")
    end
    local start_f = safe_number(clip.timeline_start_frame, 0)
    local end_f = safe_number(clip.timeline_end_frame, start_f)
    if end_f <= start_f then
        error("The selected timeline clip has no renderable duration.")
    end
    local normalized_path = output_path:gsub("\\\\", "/")
    local folder = normalized_path:match("^(.*)/[^/]+$") or "."
    local filename = normalized_path:match("([^/]+)$") or "timeline-audio.wav"
    local prefix = filename:gsub("%.wav$", "")
    mkdir_p(folder)
    local project = current_project()
    local previous_settings = nil
    local previous_format, previous_codec = nil, nil
    local job_id = nil
    local ok, result = pcall(function()
        pcall(function() previous_settings = project:GetRenderSettings() end)
        pcall(function()
            local current_format, current_codec = project:GetCurrentRenderFormatAndCodec()
            if type(current_format) == "table" then
                previous_format = current_format.format
                previous_codec = current_format.codec
            else
                previous_format = current_format
                previous_codec = current_codec
            end
        end)
        local ok_formats, formats = pcall(function() return project:GetRenderFormats() end)
        if not ok_formats or type(formats) ~= "table" then formats = {} end
        local format_entries = {}
        local known_formats = {}
        for display_format, extension in pairs(formats) do
            extension = tostring(extension):lower():gsub("^%.", "")
            if extension == "wav" or extension == "mov" or extension == "mp4" then
                table.insert(format_entries, {
                    id = extension,
                    extension = extension,
                    display = tostring(display_format),
                })
                known_formats[extension] = true
            end
        end
        -- Some Resolve builds omit usable containers from GetRenderFormats().
        -- Probe the documented extension IDs anyway; Resolve's scripting API
        -- expects IDs such as "mov", not Deliver-page labels such as "QuickTime".
        local known_candidates = {
            { id = "wav", extension = "wav", display = "Wave" },
            { id = "mov", extension = "mov", display = "QuickTime" },
            { id = "mp4", extension = "mp4", display = "MP4" },
        }
        for _, candidate in ipairs(known_candidates) do
            if not known_formats[candidate.extension] then
                table.insert(format_entries, candidate)
            end
        end
        local order = { wav = 1, mov = 2, mp4 = 3 }
        table.sort(format_entries, function(a, b)
            return (order[a.extension] or 9) < (order[b.extension] or 9)
        end)
        local selected_format, selected_codec, selected_extension = nil, nil, nil
        local tried_formats = {}
        for _, entry in ipairs(format_entries) do
            local render_format = entry.id
            local ok_codecs, codecs = pcall(function()
                return project:GetRenderCodecs(render_format)
            end)
            if not ok_codecs then codecs = {} end
            local candidates = {}
            for label, codec in pairs(codecs or {}) do
                local code = tostring(codec)
                if entry.extension == "wav" then
                    if (tostring(label) .. " " .. code):lower():find("pcm", 1, true) and code ~= "" then
                        table.insert(candidates, code)
                    end
                elseif code ~= "" then
                    table.insert(candidates, code)
                end
            end
            if #candidates == 0 and entry.extension == "wav" then
                candidates = { "LinearPCM" }
            elseif #candidates == 0 and entry.extension == "mov" then
                candidates = { "H264", "ProRes422" }
            elseif #candidates == 0 and entry.extension == "mp4" then
                candidates = { "H264" }
            end
            table.sort(candidates, function(a, b)
                local function priority(codec)
                    codec = codec:lower()
                    if codec == "h264" then return 0 end
                    if codec:find("prores422", 1, true) then return 1 end
                    return 2
                end
                return priority(a) < priority(b)
            end)
            for _, codec in ipairs(candidates) do
                table.insert(tried_formats, entry.display .. " (" .. render_format .. ")/" .. codec)
                local ok_format, accepted = pcall(function()
                    return project:SetCurrentRenderFormatAndCodec(render_format, codec)
                end)
                if ok_format and accepted then
                    selected_format = render_format
                    selected_codec = codec
                    selected_extension = entry.extension
                    break
                end
            end
            if selected_format then break end
        end
        if not selected_format then
            error("Resolve could not select a WAV or audio-only MOV render format. Tried "
                .. (#tried_formats > 0 and table.concat(tried_formats, ", ")
                    or "WAV/LinearPCM and MOV codecs"))
        end
        local rendered_path = folder .. "/" .. prefix .. "." .. selected_extension
        local settings = {
            SelectAllFrames = false,
            MarkIn = start_f,
            MarkOut = end_f - 1,
            TargetDir = folder,
            CustomName = prefix,
            ExportVideo = false,
            ExportAudio = true,
        }
        -- H.264 QuickTime rejects LinearPCM / sample-rate fields. WAV can set them.
        if selected_extension == "wav" then
            settings.AudioCodec = "LinearPCM"
            settings.AudioBitDepth = 16
            settings.AudioSampleRate = 16000
        elseif selected_extension == "mp4" then
            settings.AudioCodec = "aac"
            settings.AudioBitDepth = 16
            settings.AudioSampleRate = 16000
        end
        if not project:SetRenderSettings(settings) then
            error("Resolve rejected the selected clip audio render settings.")
        end
        job_id = project:AddRenderJob()
        if not job_id then error("Resolve could not queue audio rendering for this clip.") end
        if not project:StartRendering({ job_id }, false) then
            error("Resolve could not start audio rendering for this clip.")
        end
        local duration = (end_f - start_f) / math.max(1, safe_number(clip.fps, 24))
        local deadline = os.time() + math.max(300, duration * 10)
        while os.time() < deadline do
            local status = project:GetRenderJobStatus(job_id) or {}
            local state = tostring(status.JobStatus or ""):lower()
            if state == "complete" or state == "completed" then break end
            if state == "failed" or state == "cancelled" or state == "canceled" then
                error("Resolve failed to render selected clip audio.")
            end
            sleep(0.2)
        end
        if os.time() >= deadline then error("Resolve timed out while rendering clip audio.") end
        if file_exists(rendered_path) then return rendered_path end
        return nil
    end)
    if job_id then pcall(function() project:DeleteRenderJob(job_id) end) end
    if previous_settings then pcall(function() project:SetRenderSettings(previous_settings) end) end
    if previous_format and previous_codec then
        pcall(function() project:SetCurrentRenderFormatAndCodec(previous_format, previous_codec) end)
    end
    if not ok then
        error("Could not render selected clip audio in Resolve: " .. tostring(result))
    end
    if not result then error("Resolve finished rendering but did not create the WAV file.") end
    return result
end

local function frame_to_timecode(frame, fps)
    local fps_i = math.max(1, math.floor(fps + 0.5))
    local ff = frame % fps_i
    local ss = math.floor(frame / fps_i) % 60
    local mm = math.floor(frame / (fps_i * 60)) % 60
    local hh = math.floor(frame / (fps_i * 3600))
    return string.format("%02d:%02d:%02d:%02d", hh, mm, ss, ff)
end

list_clips = function()
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
    -- First frame at or after onset — never round backward into pre-word silence.
    local offset = math.ceil(source_offset * clip.fps - 1e-9)
    if offset < 0 then
        offset = 0
    end
    local frame = clip.timeline_start_frame + offset
    if frame < clip.timeline_start_frame then
        frame = clip.timeline_start_frame
    end
    if frame > clip.timeline_end_frame - 1 then
        frame = clip.timeline_end_frame - 1
    end
    timeline:SetCurrentTimecode(frame_to_timecode(frame, clip.fps))
    return true
end

local function jump_to_timeline_frame(frame)
    resolve:OpenPage("edit")
    local timeline = current_timeline()
    timeline:SetCurrentTimecode(frame_to_timecode(
        math.floor(safe_number(frame, 0)), timeline_fps()
    ))
    return true
end

local function find_textplus_tool(comp)
    if not comp then
        return nil
    end
    for _, name in ipairs({ "Text1", "TextPlus1", "TextPlus", "Text+", "Template" }) do
        local tool = nil
        pcall(function() tool = comp:FindTool(name) end)
        if tool then
            return tool
        end
    end
    local ok, tools = pcall(function() return comp:GetToolList(false) end)
    if not ok or type(tools) ~= "table" then
        return nil
    end
    for _, tool in pairs(tools) do
        local matched = false
        pcall(function()
            local attrs = tool:GetAttrs() or {}
            local reg = tostring(attrs.TOOLS_RegID or "")
            local name = tostring(attrs.TOOLS_Name or "")
            if reg:find("Text", 1, true) or name:find("Text", 1, true) then
                matched = true
            end
        end)
        if matched then
            return tool
        end
    end
    return nil
end

local function set_textplus_property(item, tool, property_name, value, input_name, input_value)
    local ok, result = pcall(function()
        return item:SetProperty(property_name, value)
    end)
    if ok and result then
        return true
    end
    if tool then
        local set_ok = pcall(function()
            tool[input_name or property_name] = input_value == nil and value or input_value
        end)
        if set_ok then
            return true
        end
    end
    return false
end

local function apply_caption_style(item, caption, settings)
    local comp = nil
    local tool = nil
    pcall(function()
        local count = safe_number(item:GetFusionCompCount(), 1)
        for index = 1, math.max(1, count) do
            comp = item:GetFusionCompByIndex(index)
            tool = find_textplus_tool(comp)
            if tool then
                break
            end
        end
    end)
    local function rgb(hex)
        local value = tostring(hex or "#FFFFFF"):gsub("#", "")
        if #value ~= 6 then
            return 1, 1, 1
        end
        return tonumber(value:sub(1, 2), 16) / 255,
            tonumber(value:sub(3, 4), 16) / 255,
            tonumber(value:sub(5, 6), 16) / 255
    end
    local tr, tg, tb = rgb(settings.text_color)
    local or_, og, ob = rgb(settings.outline_color)
    local sr, sg, sb = rgb(settings.shadow_color)
    local timeline = current_timeline()
    local width = safe_number(timeline:GetSetting("timelineResolutionWidth"), 1920)
    local height = safe_number(timeline:GetSetting("timelineResolutionHeight"), 1080)
    local font_size = tonumber(settings.font_size) or 96
    local vertical = tostring(settings.vertical_alignment or "center")
    vertical = vertical:sub(1, 1):upper() .. vertical:sub(2)
    local alignment = tostring(settings.alignment or "center")
    alignment = alignment:sub(1, 1):upper() .. alignment:sub(2)
    local alignment_input = ({ Left = 0, Center = 1, Right = 2 })[alignment] or 1
    local vertical_input = ({ Top = 0, Center = 1, Bottom = 2 })[vertical] or 1
    local layout_type = tostring(settings.layout_type or "Frame")
    local layout_input = layout_type == "Point" and 0 or 1
    local values = {
        { "StyledText", caption.text, "StyledText" },
        { "Font", settings.font_family or "Arial", "Font" },
        { "Style", settings.font_style or "Regular", "Style" },
        { "FontSize", font_size / math.max(1, height), "Size" },
        { "Tracking", tonumber(settings.tracking) or 1.0, "Tracking" },
        { "LineSpacing", tonumber(settings.line_spacing) or 1.0, "LineSpacing" },
        { "HorizontalJustification", alignment, "HorizontalJustification", alignment_input },
        { "PositionX", tonumber(settings.position_x) or 0.5, "Center" },
        { "PositionY", tonumber(settings.position_y) or 0.85, "Center" },
        { "AnchorPointX", ((tonumber(settings.anchor_x) or 0.5) - 0.5) * width, "AnchorPointX" },
        { "AnchorPointY", (0.5 - (tonumber(settings.anchor_y) or 0.5)) * height, "AnchorPointY" },
        { "ZoomX", tonumber(settings.scale_x) or 1.0, "ZoomX" },
        { "ZoomY", tonumber(settings.scale_y) or 1.0, "ZoomY" },
        { "RotationAngle", tonumber(settings.rotation) or 0, "Angle" },
        { "LayoutType", layout_type, "LayoutType", layout_input },
        { "Width", tonumber(settings.layout_width) or 1.0, "Width" },
        { "Height", tonumber(settings.layout_height) or 1.0, "Height" },
        { "VerticalJustification", vertical, "VerticalJustification", vertical_input },
        { "ColorRed", tr, "Red1" }, { "ColorGreen", tg, "Green1" },
        { "ColorBlue", tb, "Blue1" }, { "ColorAlpha", 1.0, "Alpha1" },
        { "OutlineEnabled", settings.outline_enabled and 1 or 0, "Enabled2" },
        { "OutlineRed", or_, "Red2" }, { "OutlineGreen", og, "Green2" },
        { "OutlineBlue", ob, "Blue2" },
        { "OutlineWidth", tonumber(settings.outline_width) or 2, "Thickness2",
            (tonumber(settings.outline_width) or 2) / math.max(1, font_size) },
        { "OutlineAlpha", tonumber(settings.outline_opacity) or 1.0, "Alpha2" },
        { "ShadowEnabled", settings.shadow_enabled and 1 or 0, "Enabled3" },
        { "ShadowRed", sr, "Red3" }, { "ShadowGreen", sg, "Green3" },
        { "ShadowBlue", sb, "Blue3" },
        { "ShadowOpacity", tonumber(settings.shadow_opacity) or 0.65, "Alpha3" },
        { "Opacity", (tonumber(settings.image_opacity) or 1.0) * 100, "Opacity" },
    }
    local required = {
        StyledText = true, Font = true, FontSize = true,
        PositionX = true, PositionY = true,
        ColorRed = true, ColorGreen = true, ColorBlue = true, ColorAlpha = true,
    }
    for _, entry in ipairs(values) do
        local applied = set_textplus_property(item, tool, entry[1], entry[2], entry[3], entry[4])
        if not applied and required[entry[1]] then
            error("Resolve could not apply Text+ setting '" .. entry[1] .. "' to a caption.")
        end
    end
    if settings.write_on and (not tool or not comp) then
        error("Resolve did not expose the Text+ controls needed for write-on.")
    end
    if settings.write_on and tool and comp then
        local duration = math.max(0, safe_number(caption.write_on_end_frame, caption.start_frame)
            - safe_number(caption.start_frame, 0))
        local ok = pcall(function()
            local spline = comp:BezierSpline()
            spline[0] = 0.0
            spline[duration] = 1.0
            tool.WriteOnEnd = spline
        end)
        if not ok then
            error("Could not animate the Text+ write-on control.")
        end
    end
end

local function create_captions(clip_id, captions, settings)
    local clip = clips_by_id[clip_id]
    if not clip then
        list_clips()
        clip = clips_by_id[clip_id]
    end
    if not clip or clip.track_type ~= "video" then
        error("Choose a video clip before creating captions.")
    end
    if not captions or #captions == 0 then
        error("There are no caption segments to create.")
    end
    local timeline = current_timeline()
    local track_count = safe_number(timeline:GetTrackCount("video"), 0)
    local added_track = false
    if track_count < 1 then
        if not timeline:AddTrack("video") then
            error("Could not create a video track for captions.")
        end
        track_count = safe_number(timeline:GetTrackCount("video"), 1)
        added_track = true
    end
    local track_index = track_count
    local overlaps = false
    local existing = timeline:GetItemListInTrack("video", track_index) or {}
    for _, item in ipairs(existing) do
        local item_start = safe_number(item:GetStart(), -1)
        local item_end = safe_number(item:GetEnd(), -1)
        for _, caption in ipairs(captions) do
            if safe_number(caption.start_frame, 0) < item_end
                and item_start < safe_number(caption.end_frame, 0) then
                overlaps = true
                break
            end
        end
        if overlaps then break end
    end
    if overlaps then
        if not timeline:AddTrack("video") then
            error("The top video track is occupied and Resolve could not add a new one.")
        end
        track_index = safe_number(timeline:GetTrackCount("video"), track_count + 1)
        added_track = true
    end

    local inserted = {}
    local template_seeds = {}
    local place_mode = nil
    local function item_span(item)
        local start_at, end_at, track = -1, -1, -1
        if not item then
            return start_at, end_at, track
        end
        pcall(function() start_at = safe_number(item:GetStart(), -1) end)
        pcall(function() end_at = safe_number(item:GetEnd(), -1) end)
        pcall(function()
            local info = item:GetTrackTypeAndIndex()
            if type(info) == "table" then
                track = safe_number(info[2], -1)
            end
        end)
        return start_at, end_at, track
    end
    local function span_fits(got_start, got_end, start_f, end_f)
        return math.abs(got_start - start_f) <= 1 and math.abs(got_end - end_f) <= 1
    end
    local function track_fits(got_track)
        return got_track < 0 or got_track == track_index
    end
    local function go_to(frame)
        local moved = false
        pcall(function()
            moved = timeline:SetCurrentTimecode(
                frame_to_timecode(math.floor(frame), clip.fps)
            )
        end)
        return moved and true or false
    end
    local function insert_textplus()
        local ok_insert, item = pcall(function()
            return timeline:InsertFusionTitleIntoTimeline("Text+")
        end)
        if ok_insert and item then
            return item
        end
        return nil
    end
    local comp_path = nil
    local carrier_mp = nil
    local function first_item(result)
        if type(result) == "table" then
            return result[1]
        end
        if result and type(result) ~= "boolean" and type(result) ~= "string" and type(result) ~= "number" then
            return result
        end
        return nil
    end
    local function prepare_carrier()
        if carrier_mp and comp_path then
            return
        end
        local tl_end = 0
        pcall(function() tl_end = safe_number(timeline:GetEndFrame(), 0) end)
        go_to(tl_end)
        local seeded = insert_textplus()
        if not seeded then
            error("Resolve could not insert a Text+ title.")
        end
        local comp_count = 0
        pcall(function() comp_count = safe_number(seeded:GetFusionCompCount(), 0) end)
        mkdir_p(data_dir())
        comp_path = data_dir() .. "/caption-textplus.comp"
        local export_ok, exported = false, nil
        if comp_count > 0 then
            export_ok, exported = pcall(function()
                return seeded:ExportFusionComp(comp_path, 1)
            end)
        end
        pcall(function() timeline:DeleteClips({ seeded }, false) end)
        local comp_file = file_exists(comp_path)
        if not (export_ok and exported and comp_file) then
            error("Resolve could not export the Text+ composition used for captions.")
        end
        local max_frames = 1
        for _, caption in ipairs(captions) do
            local span = safe_number(caption.end_frame, 0) - safe_number(caption.start_frame, 0)
            if span > max_frames then
                max_frames = span
            end
        end
        local fps = math.max(1, math.floor(safe_number(clip.fps, 24) + 0.5))
        local video_path = data_dir() .. "/caption-carrier.mov"
        local ffmpeg_bin = "ffmpeg"
        for _, candidate in ipairs({ "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "ffmpeg" }) do
            local probe = io.popen(candidate .. " -version 2>&1", "r")
            local header = probe and probe:read("*l") or nil
            if probe then probe:close() end
            if header and header:find("ffmpeg", 1, true) then
                ffmpeg_bin = candidate
                break
            end
        end
        local quoted = "'" .. video_path:gsub("'", "'\\''") .. "'"
        local cmd = string.format(
            "%s -y -f lavfi -i color=c=black:s=16x16:r=%d:d=%.6f -pix_fmt yuv420p -c:v libx264 %s",
            ffmpeg_bin:find("/", 1, true) and ("'" .. ffmpeg_bin .. "'") or ffmpeg_bin,
            fps,
            max_frames / fps,
            quoted
        )
        os.execute(cmd)
        local video_file = file_exists(video_path)
        local media_pool = current_project():GetMediaPool()
        local root = media_pool:GetRootFolder()
        local captain_bin = nil
        for _, folder in ipairs(root:GetSubFolderList() or {}) do
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
        local ok_import, imported = false, nil
        if video_file then
            ok_import, imported = pcall(function()
                return media_pool:ImportMedia({ video_path })
            end)
            carrier_mp = ok_import and first_item(imported) or nil
        end
        if not carrier_mp then
            error("Resolve could not import the caption carrier video.")
        end
    end
    local function append_still(record_frame, source_frames)
        local media_pool = current_project():GetMediaPool()
        local ok_append, appended = pcall(function()
            return media_pool:AppendToTimeline({ {
                mediaPoolItem = carrier_mp,
                startFrame = 0,
                endFrame = source_frames,
                trackIndex = track_index,
                recordFrame = record_frame,
                mediaType = 1,
            } })
        end)
        if ok_append then
            return first_item(appended)
        end
        return nil
    end
    local function place_caption_title(start_f, end_f)
        local duration = end_f - start_f
        if type(timeline.AddFusionTitleClip) == "function" then
            return timeline:AddFusionTitleClip("Text+", track_index, start_f, duration)
        end
        prepare_carrier()
        local placed = append_still(start_f, duration)
        local got_start, got_end, got_track = item_span(placed)
        local fits = placed ~= nil and span_fits(got_start, got_end, start_f, end_f) and track_fits(got_track)
        local ok_comp, comp = false, nil
        if fits then
            ok_comp, comp = pcall(function()
                return placed:ImportFusionComp(comp_path)
            end)
        end
        if not fits then
            if placed then
                pcall(function() timeline:DeleteClips({ placed }, false) end)
            end
            error(
                "Resolve placed the caption at "
                .. tostring(got_start) .. "-" .. tostring(got_end)
                .. " on track " .. tostring(got_track)
                .. " instead of " .. tostring(start_f) .. "-" .. tostring(end_f)
                .. " on track " .. tostring(track_index) .. "."
            )
        end
        if not ok_comp or not comp then
            pcall(function() timeline:DeleteClips({ placed }, false) end)
            error("Resolve could not attach the Text+ composition to the caption.")
        end
        return placed
    end
    local ok, count_or_error = pcall(function()
        for _, caption in ipairs(captions) do
            local start_f = safe_number(caption.start_frame, -1)
            local end_f = safe_number(caption.end_frame, -1)
            if end_f > start_f then
                local item = place_caption_title(start_f, end_f)
                if not item then
                    error("Resolve could not create a caption at frame " .. tostring(start_f))
                end
                inserted[#inserted + 1] = item
                apply_caption_style(item, caption, settings or {})
            end
        end
        if #inserted == 0 then
            error("Resolve did not create any caption titles.")
        end
        return #inserted
    end)
    if #template_seeds > 0 then
        pcall(function() timeline:DeleteClips(template_seeds, false) end)
    end
    if not ok then
        if #inserted > 0 then
            pcall(function() timeline:DeleteClips(inserted, false) end)
        end
        if added_track then
            local remaining = timeline:GetItemListInTrack("video", track_index) or {}
            if #remaining == 0 then
                pcall(function() timeline:DeleteTrack("video", track_index) end)
            end
        end
        error(count_or_error)
    end
    clips_by_id = {}
    return count_or_error
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

local function find_item_track(timeline, item)
    for _, tt in ipairs({ "video", "audio" }) do
        local count = timeline:GetTrackCount(tt) or 0
        for idx = 1, count do
            for _, it in ipairs(timeline:GetItemListInTrack(tt, idx) or {}) do
                if it == item then
                    return tt, idx
                end
            end
        end
    end
    return nil, nil
end

local function replace_clip_in_place(clip_id, keep_ranges_frames, ripple)
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
    if ripple == nil then
        ripple = false
    end
    local timeline = current_timeline()
    local record_frame = safe_number(clip._item:GetStart(), clip.timeline_start_frame)

    -- Primary + linked A/V must be deleted and re-inserted together. Otherwise
    -- ripple drops audio entirely, and non-ripple leaves full-length audio.
    local to_delete = { clip._item }
    local video_tracks = {}
    local audio_tracks = {}
    local function add_track(tt, idx)
        if not tt or not idx then
            return
        end
        local list = (tt == "audio") and audio_tracks or video_tracks
        for _, existing in ipairs(list) do
            if existing == idx then
                return
            end
        end
        table.insert(list, idx)
    end
    add_track(clip.track_type, clip.track_index)

    local ok_linked, linked = pcall(function()
        return clip._item:GetLinkedItems()
    end)
    if ok_linked and linked then
        for _, li in ipairs(linked) do
            local li_tt, li_idx = find_item_track(timeline, li)
            add_track(li_tt, li_idx)
            table.insert(to_delete, li)
        end
    end

    if not timeline:DeleteClips(to_delete, ripple and true or false) then
        error("Failed to delete clip '" .. tostring(clip.name) .. "' from the timeline.")
    end
    local media_pool = current_project():GetMediaPool()
    local entries = {}
    local rf = record_frame
    for _, range in ipairs(keep_ranges_frames) do
        local start_f = safe_number(range[1], 0)
        local end_f = safe_number(range[2], 0)
        local duration = math.max(0, end_f - start_f)
        for _, vidx in ipairs(video_tracks) do
            table.insert(entries, {
                mediaPoolItem = clip._mp,
                startFrame = start_f,
                endFrame = end_f,
                trackIndex = vidx,
                recordFrame = rf,
                mediaType = 1,
            })
        end
        for _, aidx in ipairs(audio_tracks) do
            table.insert(entries, {
                mediaPoolItem = clip._mp,
                startFrame = start_f,
                endFrame = end_f,
                trackIndex = aidx,
                recordFrame = rf,
                mediaType = 2,
            })
        end
        -- Audio-only or video-only clip with no opposite track still needs one entry.
        if #video_tracks == 0 and #audio_tracks == 0 then
            local media_type = (clip.track_type == "audio") and 2 or 1
            table.insert(entries, {
                mediaPoolItem = clip._mp,
                startFrame = start_f,
                endFrame = end_f,
                trackIndex = clip.track_index,
                recordFrame = rf,
                mediaType = media_type,
            })
        end
        rf = rf + duration
    end

    local inserted = {}
    local i = 1
    while i <= #entries do
        local chunk = {}
        for j = i, math.min(i + 49, #entries) do
            table.insert(chunk, entries[j])
        end
        local appended = media_pool:AppendToTimeline(chunk)
        if not appended then
            return false
        end
        if type(appended) == "table" then
            for _, it in ipairs(appended) do
                table.insert(inserted, it)
            end
        end
        i = i + 50
    end
    if #video_tracks > 0 and #audio_tracks > 0 then
        local group_size = #video_tracks + #audio_tracks
        if #inserted == #entries then
            -- Preferred: link the exact items AppendToTimeline returned. Entries
            -- are built per keep range (video then audio), so slice per range.
            -- Position-based matching is unreliable after a ripple delete shifts
            -- timeline content.
            for g = 1, #inserted, group_size do
                local group = {}
                for j = g, math.min(g + group_size - 1, #inserted) do
                    table.insert(group, inserted[j])
                end
                if #group >= 2 and timeline:SetClipsLinked(group, true) == false then
                    script_log("SetClipsLinked failed for inserted group at " .. g)
                end
            end
        else
            -- Fallback: match by source range only, then group by the actual
            -- timeline start of each copy (do not assume where clips landed).
            local link_rf = record_frame
            for _, range in ipairs(keep_ranges_frames) do
                local start_f = safe_number(range[1], 0)
                local end_f = safe_number(range[2], 0)
                local duration = math.max(0, end_f - start_f)
                local by_start = {}
                for _, track_info in ipairs({
                    { trackType = "video", indices = video_tracks },
                    { trackType = "audio", indices = audio_tracks },
                }) do
                    for _, idx in ipairs(track_info.indices) do
                        for _, item in ipairs(timeline:GetItemListInTrack(track_info.trackType, idx) or {}) do
                            if safe_number(item:GetSourceStartFrame(), -1) == start_f
                                and safe_number(item:GetSourceEndFrame(), -1) == end_f then
                                local s = safe_number(item:GetStart(), -1)
                                by_start[s] = by_start[s] or {}
                                table.insert(by_start[s], item)
                            end
                        end
                    end
                end
                local best_start, best_group = nil, nil
                for s, group in pairs(by_start) do
                    if #group >= 2 and (best_start == nil
                        or math.abs(s - link_rf) < math.abs(best_start - link_rf)) then
                        best_start, best_group = s, group
                    end
                end
                if best_group then
                    if timeline:SetClipsLinked(best_group, true) == false then
                        script_log("SetClipsLinked failed for range "
                            .. start_f .. "-" .. end_f)
                    end
                else
                    script_log("No linkable items found for range "
                        .. start_f .. "-" .. end_f)
                end
                link_rf = link_rf + duration
            end
        end
    end
    clips_by_id = {}
    return true
end

local function list_timeline_names()
    local project = current_project()
    local names = {}
    local count = safe_number(project:GetTimelineCount(), 0)
    for i = 1, count do
        local timeline = project:GetTimelineByIndex(i)
        if timeline then
            table.insert(names, timeline:GetName() or "")
        end
    end
    return names
end

local function dispatch(method, params)
    params = params or {}
    if method == "ping" then
        return { ok = true, version = version_string(), mode = "lua-file" }
    elseif method == "timeline_name" then
        return current_timeline():GetName()
    elseif method == "list_timeline_names" then
        return list_timeline_names()
    elseif method == "timeline_fps" then
        return timeline_fps()
    elseif method == "timeline_info" then
        return timeline_info()
    elseif method == "capture_current_frame" then
        return capture_current_frame(params.image_path)
    elseif method == "render_clip_audio" then
        return render_clip_audio(params.clip_id, params.output_path)
    elseif method == "list_clips" then
        return list_clips()
    elseif method == "clip_under_playhead" then
        return clip_under_playhead()
    elseif method == "jump_to_clip_second" then
        return jump_to_clip_second(params.clip_id, safe_number(params.second_in_clip, 0))
    elseif method == "jump_to_timeline_frame" then
        return jump_to_timeline_frame(params.frame)
    elseif method == "import_timeline_xml" then
        return import_timeline_xml(params.xml_path)
    elseif method == "assemble_append" then
        return assemble_append(params.clip_id, params.keep_ranges_frames, params.new_name)
    elseif method == "replace_clip_in_place" then
        return replace_clip_in_place(
            params.clip_id,
            params.keep_ranges_frames,
            params.ripple
        )
    elseif method == "create_captions" then
        return create_captions(params.clip_id, params.captions, params.settings)
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
