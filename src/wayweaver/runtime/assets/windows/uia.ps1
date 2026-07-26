param(
    [Parameter(Mandatory = $true)]
    [string]$Action
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

function Write-Json($Value) {
    [Console]::Out.WriteLine(($Value | ConvertTo-Json -Compress -Depth 16))
}

function Get-Option($Options, [string]$Name, $Default = $null) {
    if ($null -ne $Options -and $null -ne $Options.PSObject.Properties[$Name]) {
        return $Options.$Name
    }
    return $Default
}

function Get-Pattern($Element, $Pattern) {
    $value = $null
    if ($Element.TryGetCurrentPattern($Pattern, [ref]$value)) {
        return $value
    }
    return $null
}

function Get-States($Element) {
    $states = [System.Collections.Generic.List[string]]::new()
    if ($Element.Current.IsEnabled) { $states.Add('enabled') }
    if ($Element.Current.HasKeyboardFocus) { $states.Add('focused') }
    if ($Element.Current.IsKeyboardFocusable) { $states.Add('keyboard-focusable') }
    if ($Element.Current.IsOffscreen) { $states.Add('offscreen') } else { $states.Add('visible') }
    if ($Element.Current.IsPassword) { $states.Add('password') }
    $toggle = Get-Pattern $Element ([System.Windows.Automation.TogglePattern]::Pattern)
    if ($null -ne $toggle) {
        $states.Add($toggle.Current.ToggleState.ToString().ToLowerInvariant())
    }
    $selection = Get-Pattern $Element ([System.Windows.Automation.SelectionItemPattern]::Pattern)
    if ($null -ne $selection -and $selection.Current.IsSelected) { $states.Add('selected') }
    return @($states)
}

function Get-Actions($Element) {
    $actions = [System.Collections.Generic.List[string]]::new()
    if ($null -ne (Get-Pattern $Element ([System.Windows.Automation.InvokePattern]::Pattern))) { $actions.Add('invoke') }
    if ($null -ne (Get-Pattern $Element ([System.Windows.Automation.TogglePattern]::Pattern))) { $actions.Add('toggle') }
    if ($null -ne (Get-Pattern $Element ([System.Windows.Automation.SelectionItemPattern]::Pattern))) { $actions.Add('select') }
    if ($null -ne (Get-Pattern $Element ([System.Windows.Automation.ExpandCollapsePattern]::Pattern))) { $actions.Add('expand-collapse') }
    if ($null -ne (Get-Pattern $Element ([System.Windows.Automation.ValuePattern]::Pattern))) { $actions.Add('set-value') }
    if ($null -ne (Get-Pattern $Element ([System.Windows.Automation.RangeValuePattern]::Pattern))) { $actions.Add('set-range') }
    return @($actions)
}

function Describe-Element($Element, [int[]]$Path) {
    $bounds = $Element.Current.BoundingRectangle
    $role = $Element.Current.ControlType.ProgrammaticName -replace '^ControlType\.', ''
    return [ordered]@{
        path = @($Path)
        name = $Element.Current.Name
        role = $role.ToLowerInvariant()
        description = $Element.Current.HelpText
        automation_id = $Element.Current.AutomationId
        class = $Element.Current.ClassName
        pid = $Element.Current.ProcessId
        states = @(Get-States $Element)
        actions = @(Get-Actions $Element)
        bounds = [ordered]@{
            left = [int]$bounds.X
            top = [int]$bounds.Y
            width = [int]$bounds.Width
            height = [int]$bounds.Height
        }
    }
}

function Get-Nodes([int]$MaxDepth, [int]$Limit) {
    $walker = [System.Windows.Automation.TreeWalker]::RawViewWalker
    $queue = [System.Collections.Generic.Queue[object]]::new()
    $queue.Enqueue([pscustomobject]@{
        element = [System.Windows.Automation.AutomationElement]::RootElement
        path = [int[]]@()
        depth = 0
    })
    $result = [System.Collections.Generic.List[object]]::new()
    while ($queue.Count -gt 0 -and $result.Count -lt $Limit) {
        $entry = $queue.Dequeue()
        try {
            $result.Add($entry)
            if ($entry.depth -ge $MaxDepth) { continue }
            $child = $walker.GetFirstChild($entry.element)
            $index = 0
            while ($null -ne $child) {
                $queue.Enqueue([pscustomobject]@{
                    element = $child
                    path = [int[]]@($entry.path + $index)
                    depth = $entry.depth + 1
                })
                $child = $walker.GetNextSibling($child)
                $index++
            }
        } catch [System.Windows.Automation.ElementNotAvailableException] {
            continue
        }
    }
    return @($result)
}

function Test-Match($Element, $Options) {
    try {
        $name = [string]$Element.Current.Name
        $role = ([string]$Element.Current.ControlType.ProgrammaticName -replace '^ControlType\.', '').ToLowerInvariant()
        $wantedName = [string](Get-Option $Options 'name' (Get-Option $Options 'text' ''))
        $wantedRole = ([string](Get-Option $Options 'role' '')).ToLowerInvariant()
        $wantedId = [string](Get-Option $Options 'automation_id' (Get-Option $Options 'id' ''))
        $wantedClass = [string](Get-Option $Options 'class' '')
        $wantedPid = Get-Option $Options 'pid' $null
        $exact = [bool](Get-Option $Options 'exact' $false)
        if ($wantedName) {
            if ($exact -and $name -cne $wantedName) { return $false }
            if (-not $exact -and $name.IndexOf($wantedName, [StringComparison]::OrdinalIgnoreCase) -lt 0) { return $false }
        }
        if ($wantedRole -and $role -ne $wantedRole) { return $false }
        if ($wantedId -and $Element.Current.AutomationId -ne $wantedId) { return $false }
        if ($wantedClass -and $Element.Current.ClassName -ne $wantedClass) { return $false }
        if ($null -ne $wantedPid -and $Element.Current.ProcessId -ne [int]$wantedPid) { return $false }
        if (-not [bool](Get-Option $Options 'include_offscreen' $false) -and $Element.Current.IsOffscreen) { return $false }
        return [bool]($wantedName -or $wantedRole -or $wantedId -or $wantedClass -or $null -ne $wantedPid)
    } catch [System.Windows.Automation.ElementNotAvailableException] {
        return $false
    }
}

function Find-Records($Options) {
    $maxDepth = [int](Get-Option $Options 'max_depth' 14)
    $limit = [int](Get-Option $Options 'limit' 2000)
    return @(Get-Nodes $maxDepth $limit | Where-Object { Test-Match $_.element $Options })
}

function Select-Record($Options) {
    $matches = @(Find-Records $Options)
    $nth = [int](Get-Option $Options 'nth' 0)
    if ($nth -lt 0) { $nth = $matches.Count + $nth }
    if ($nth -lt 0 -or $nth -ge $matches.Count) {
        throw "UI Automation element not found"
    }
    return [pscustomobject]@{ record = $matches[$nth]; count = $matches.Count }
}

function Test-ExpectedState($Element, $Options) {
    $state = [string](Get-Option $Options 'state' '')
    if (-not $state) { return $true }
    $expected = [bool](Get-Option $Options 'value' $true)
    $present = (Get-States $Element) -contains $state.ToLowerInvariant()
    return $present -eq $expected
}

function Wait-Record($Options) {
    $timeout = [double](Get-Option $Options 'timeout' 5)
    $signal = [System.Threading.AutoResetEvent]::new($false)
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $structureHandler = [System.Windows.Automation.StructureChangedEventHandler]{
        param($sender, $eventArgs)
        $signal.Set() | Out-Null
    }
    $propertyHandler = [System.Windows.Automation.AutomationPropertyChangedEventHandler]{
        param($sender, $eventArgs)
        $signal.Set() | Out-Null
    }
    $focusHandler = [System.Windows.Automation.AutomationFocusChangedEventHandler]{
        param($sender, $eventArgs)
        $signal.Set() | Out-Null
    }
    [System.Windows.Automation.Automation]::AddStructureChangedEventHandler(
        $root,
        [System.Windows.Automation.TreeScope]::Subtree,
        $structureHandler)
    [System.Windows.Automation.Automation]::AddAutomationPropertyChangedEventHandler(
        $root,
        [System.Windows.Automation.TreeScope]::Subtree,
        $propertyHandler,
        @(
            [System.Windows.Automation.AutomationElement]::NameProperty,
            [System.Windows.Automation.AutomationElement]::IsEnabledProperty,
            [System.Windows.Automation.AutomationElement]::IsOffscreenProperty,
            [System.Windows.Automation.AutomationElement]::HasKeyboardFocusProperty
        ))
    [System.Windows.Automation.Automation]::AddAutomationFocusChangedEventHandler($focusHandler)
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        while ($true) {
            try {
                $selection = Select-Record $Options
                if (Test-ExpectedState $selection.record.element $Options) {
                    return $selection
                }
            } catch {
                if ($watch.Elapsed.TotalSeconds -ge $timeout) { throw }
            }
            $remaining = [Math]::Max(0, $timeout - $watch.Elapsed.TotalSeconds)
            if ($remaining -le 0) { throw "UI Automation wait timed out" }
            $signal.WaitOne([Math]::Min(1000, [int]($remaining * 1000))) | Out-Null
        }
    } finally {
        [System.Windows.Automation.Automation]::RemoveStructureChangedEventHandler($root, $structureHandler)
        [System.Windows.Automation.Automation]::RemoveAutomationPropertyChangedEventHandler($root, $propertyHandler)
        [System.Windows.Automation.Automation]::RemoveAutomationFocusChangedEventHandler($focusHandler)
        $signal.Dispose()
    }
}

try {
    $encodedOptions = [Console]::In.ReadToEnd()
    $options = if ([string]::IsNullOrWhiteSpace($encodedOptions)) {
        [pscustomobject]@{}
    } else {
        $encodedOptions | ConvertFrom-Json
    }

    if ($Action -eq 'probe') {
        $root = [System.Windows.Automation.AutomationElement]::RootElement
        Write-Json ([ordered]@{ name = $root.Current.Name; available = $true })
        exit 0
    }

    if ($Action -eq 'focused') {
        # Typing goes wherever focus happens to be. Without a way to ask what
        # that is, a click that missed its target types into nothing and no
        # layer notices.
        $element = [System.Windows.Automation.AutomationElement]::FocusedElement
        if ($null -eq $element) { throw 'no element currently has keyboard focus' }
        $result = Describe-Element $element @()
        $value = Get-Pattern $element ([System.Windows.Automation.ValuePattern]::Pattern)
        if ($null -ne $value) { $result.text = $value.Current.Value }
        Write-Json $result
        exit 0
    }

    if ($Action -eq 'list') {
        $maxDepth = [int](Get-Option $options 'max_depth' 8)
        $limit = [int](Get-Option $options 'limit' 500)
        $nodes = @(Get-Nodes $maxDepth $limit)
        $elements = @($nodes | ForEach-Object { Describe-Element $_.element $_.path })
        Write-Json ([ordered]@{ elements = $elements; truncated = $nodes.Count -ge $limit })
        exit 0
    }

    $selection = if ($Action -in @('wait', 'wait-state')) {
        Wait-Record $options
    } else {
        Select-Record $options
    }
    $record = $selection.record
    $element = $record.element
    $result = Describe-Element $element $record.path
    $result.matches = $selection.count

    switch ($Action) {
        { $_ -in @('find', 'assert', 'wait', 'wait-state') } { }
        'read' {
            $value = Get-Pattern $element ([System.Windows.Automation.ValuePattern]::Pattern)
            if ($null -ne $value) { $result.value = $value.Current.Value }
            $text = Get-Pattern $element ([System.Windows.Automation.TextPattern]::Pattern)
            if ($null -ne $text) { $result.text = $text.DocumentRange.GetText(-1) }
        }
        'focus' {
            $element.SetFocus()
            $result.focused = $true
        }
        'invoke' {
            $invoke = Get-Pattern $element ([System.Windows.Automation.InvokePattern]::Pattern)
            $toggle = Get-Pattern $element ([System.Windows.Automation.TogglePattern]::Pattern)
            $selectionItem = Get-Pattern $element ([System.Windows.Automation.SelectionItemPattern]::Pattern)
            $expand = Get-Pattern $element ([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
            $legacy = Get-Pattern $element ([System.Windows.Automation.LegacyIAccessiblePattern]::Pattern)
            if ($null -ne $invoke) { $invoke.Invoke(); $result.invoked = 'invoke' }
            elseif ($null -ne $toggle) { $toggle.Toggle(); $result.invoked = 'toggle' }
            elseif ($null -ne $selectionItem) { $selectionItem.Select(); $result.invoked = 'select' }
            elseif ($null -ne $expand) { $expand.Expand(); $result.invoked = 'expand' }
            elseif ($null -ne $legacy) { $legacy.DoDefaultAction(); $result.invoked = 'legacy-default' }
            else { throw 'element exposes no activatable UI Automation pattern' }
        }
        'set-value' {
            $textValue = Get-Option $options 'text' $null
            $numericValue = Get-Option $options 'value' $null
            $valuePattern = Get-Pattern $element ([System.Windows.Automation.ValuePattern]::Pattern)
            $rangePattern = Get-Pattern $element ([System.Windows.Automation.RangeValuePattern]::Pattern)
            if ($null -ne $textValue -and $null -ne $valuePattern) {
                $valuePattern.SetValue([string]$textValue)
                $result.value = [string]$textValue
            } elseif ($null -ne $numericValue -and $null -ne $rangePattern) {
                $rangePattern.SetValue([double]$numericValue)
                $result.value = [double]$numericValue
            } else {
                throw 'element exposes no compatible writable UI Automation pattern'
            }
        }
        default { throw "unknown action: $Action" }
    }
    Write-Json $result
} catch {
    [Console]::Error.WriteLine((@{ error = $_.Exception.Message } | ConvertTo-Json -Compress))
    exit 1
}
