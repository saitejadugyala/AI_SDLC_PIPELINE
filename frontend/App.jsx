import React, { useState, useEffect } from 'react';

// TaskForm component for creating and editing tasks
function TaskForm({ initialTask = { title: '', completed: false }, onSubmit, onCancel }) {
  const [title, setTitle] = useState(initialTask.title);

  const handleSubmit = (e) => {
    e.preventDefault();
    if (title.trim() === '') return;
    onSubmit({ ...initialTask, title: title.trim() });
    setTitle('');
  };

  return (
    <form onSubmit={handleSubmit} style={{ marginBottom: '1rem' }}>
      <input
        type="text"
        placeholder="Enter task title"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        autoFocus
        style={{ padding: '0.5rem', width: '70%', marginRight: '0.5rem' }}
      />
      <button type="submit">Save</button>
      {onCancel && (
        <button type="button" onClick={onCancel} style={{ marginLeft: '0.5rem' }}>
          Cancel
        </button>
      )}
    </form>
  );
}

// TaskItem component displays a single task with controls for editing, deleting, toggling status
function TaskItem({ task, onUpdate, onDelete, onToggle }) {
  const [isEditing, setIsEditing] = useState(false);

  const handleEditSubmit = (updatedTask) => {
    onUpdate(updatedTask);
    setIsEditing(false);
  };

  if (isEditing) {
    return (
      <li style={{ marginBottom: '0.5rem' }}>
        <TaskForm
          initialTask={task}
          onSubmit={handleEditSubmit}
          onCancel={() => setIsEditing(false)}
        />
      </li>
    );
  }

  return (
    <li style={{ marginBottom: '0.5rem', display: 'flex', alignItems: 'center' }}>
      <input
        type="checkbox"
        checked={task.completed}
        onChange={() => onToggle(task)}
        style={{ marginRight: '0.5rem' }}
      />
      <span
        style={{
          textDecoration: task.completed ? 'line-through' : 'none',
          flexGrow: 1
        }}
      >
        {task.title}
      </span>
      <button onClick={() => setIsEditing(true)} style={{ marginRight: '0.25rem' }}>
        Edit
      </button>
      <button onClick={() => onDelete(task)}>Delete</button>
    </li>
  );
}

// TaskList component renders list of tasks
function TaskList({ tasks, onUpdate, onDelete, onToggle }) {
  if (tasks.length === 0) {
    return <p>No tasks found.</p>;
  }

  return (
    <ul style={{ listStyle: 'none', padding: 0 }}>
      {tasks.map((task) => (
        <TaskItem
          key={task.id}
          task={task}
          onUpdate={onUpdate}
          onDelete={onDelete}
          onToggle={onToggle}
        />
      ))}
    </ul>
  );
}

// Main root App component
export default function App() {
  const [tasks, setTasks] = useState([]);

  // Fetch tasks from the backend API
  useEffect(() => {
    fetch('/api/tasks')
      .then((res) => res.json())
      .then(setTasks)
      .catch(() => setTasks([]));
  }, []);

  // Handle create task
  const handleCreateTask = (task) => {
    fetch('/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: task.title, completed: task.completed })
    })
      .then((res) => res.json())
      .then((newTask) => setTasks((prev) => [...prev, newTask]))
      .catch(() => {});
  };

  // Handle update task
  const handleUpdateTask = (updatedTask) => {
    fetch(`/api/tasks/${updatedTask.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(updatedTask)
    })
      .then((res) => res.json())
      .then((newTask) => {
        setTasks((prev) => prev.map((t) => (t.id === newTask.id ? newTask : t)));
      })
      .catch(() => {});
  };

  // Handle delete task
  const handleDeleteTask = (task) => {
    fetch(`/api/tasks/${task.id}`, {
      method: 'DELETE'
    })
      .then(() => {
        setTasks((prev) => prev.filter((t) => t.id !== task.id));
      })
      .catch(() => {});
  };

  // Handle toggle task completed status using backend toggle endpoint
  const handleToggleTaskStatus = (task) => {
    fetch(`/api/tasks/${task.id}/toggle`, {
      method: 'PATCH'
    })
      .then((res) => res.json())
      .then((toggledTask) => {
        setTasks((prev) => prev.map((t) => (t.id === toggledTask.id ? toggledTask : t)));
      })
      .catch(() => {});
  };

  return (
    <div style={{ maxWidth: 600, margin: '2rem auto', padding: '1rem', fontFamily: 'Arial, sans-serif' }}>
      <h1>To-Do List</h1>
      <TaskForm onSubmit={handleCreateTask} />
      <TaskList
        tasks={tasks}
        onUpdate={handleUpdateTask}
        onDelete={handleDeleteTask}
        onToggle={handleToggleTaskStatus}
      />
    </div>
  );
}
