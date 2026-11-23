const API_BASE_URL = '/api/tasks';

// Fetch all tasks
export async function fetchTasks() {
  const response = await fetch(API_BASE_URL);
  if (!response.ok) {
    throw new Error('Failed to fetch tasks');
  }
  const tasks = await response.json();
  return tasks;
}

// Create a new task with given data { title, description? }
export async function createTask(taskData) {
  const response = await fetch(API_BASE_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(taskData),
  });
  if (!response.ok) {
    throw new Error('Failed to create task');
  }
  const newTask = await response.json();
  return newTask;
}

// Update an existing task by id with partial or full task data
export async function updateTask(taskId, updatedData) {
  const response = await fetch(`${API_BASE_URL}/${taskId}`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(updatedData),
  });
  if (!response.ok) {
    throw new Error('Failed to update task');
  }
  const updatedTask = await response.json();
  return updatedTask;
}

// Delete a task by ID
export async function deleteTask(taskId) {
  const response = await fetch(`${API_BASE_URL}/${taskId}`, {
    method: 'DELETE',
  });
  if (!response.ok) {
    throw new Error('Failed to delete task');
  }
  return true;
}

// Toggle task completion status using backend dedicated toggle endpoint
export async function toggleTaskStatus(taskId) {
  const response = await fetch(`${API_BASE_URL}/${taskId}/toggle`, {
    method: 'PATCH',
  });
  if (!response.ok) {
    throw new Error('Failed to toggle task status');
  }
  const toggledTask = await response.json();
  return toggledTask;
}
